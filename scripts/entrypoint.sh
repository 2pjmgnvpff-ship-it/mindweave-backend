#!/bin/sh
set -eu

PG_BIN_DIR="/usr/lib/postgresql/15/bin"
LOCAL_PGDATA="${MINDWEAVE_LOCAL_PGDATA:-/tmp/mindweave-pgdata}"
LOCAL_PGPORT="${MINDWEAVE_LOCAL_PGPORT:-55432}"
EMBEDDED_PG_STARTED="false"

mkdir -p "${MINDWEAVE_BLOB_STORAGE_DIR:-/tmp/mindweave-blobs}"

# Auto-detect live AI provider only when MINDWEAVE_AI_PRIMARY_PROVIDER is unset.
# Never silently inject `fake` as fallback outside explicit dev override.
if [ -z "${MINDWEAVE_AI_PRIMARY_PROVIDER:-}" ]; then
  if [ -n "${MINDWEAVE_GEMINI_API_KEY:-}" ] || [ "${MINDWEAVE_GEMINI_USE_VERTEXAI:-false}" = "true" ]; then
    export MINDWEAVE_AI_PRIMARY_PROVIDER="gemini"
    export MINDWEAVE_AI_FALLBACK_PROVIDER="${MINDWEAVE_AI_FALLBACK_PROVIDER:-none}"
    export MINDWEAVE_GEMINI_PAID_TIER_ACKNOWLEDGED="${MINDWEAVE_GEMINI_PAID_TIER_ACKNOWLEDGED:-true}"
  elif [ -n "${MINDWEAVE_DEEPSEEK_API_KEY:-}" ]; then
    export MINDWEAVE_AI_PRIMARY_PROVIDER="deepseek"
    export MINDWEAVE_AI_FALLBACK_PROVIDER="${MINDWEAVE_AI_FALLBACK_PROVIDER:-none}"
  elif [ -n "${MINDWEAVE_DASHSCOPE_API_KEY:-}" ]; then
    export MINDWEAVE_AI_PRIMARY_PROVIDER="dashscope"
    export MINDWEAVE_AI_FALLBACK_PROVIDER="${MINDWEAVE_AI_FALLBACK_PROVIDER:-none}"
  fi
fi

# Mode B (Embedded PostgreSQL 15):
# When MINDWEAVE_APP_DATABASE_URL is not configured externally, automatically boot an embedded
# PostgreSQL 15 instance inside the container with full FORCE RLS roles & Alembic schema.
if [ -z "${MINDWEAVE_APP_DATABASE_URL:-}" ] && [ -x "$PG_BIN_DIR/initdb" ]; then
  if ! "$PG_BIN_DIR/pg_isready" -h 127.0.0.1 -p "$LOCAL_PGPORT" >/dev/null 2>&1; then
    echo "[mindweave] No external database configured; bootstrapping embedded PostgreSQL 15..."
    if [ ! -s "$LOCAL_PGDATA/PG_VERSION" ]; then
      mkdir -p "$LOCAL_PGDATA"
      chmod 700 "$LOCAL_PGDATA"
      "$PG_BIN_DIR/initdb" -D "$LOCAL_PGDATA" --username=postgres --auth=trust --encoding=UTF8 --locale=C.UTF-8 >/dev/null
    fi
    rm -f "$LOCAL_PGDATA/postmaster.pid"
    "$PG_BIN_DIR/postgres" -D "$LOCAL_PGDATA" -p "$LOCAL_PGPORT" -h 127.0.0.1 -k /tmp -c dynamic_shared_memory_type=mmap -c shared_buffers=64MB &
    EMBEDDED_PG_STARTED="true"

    i=0
    while ! "$PG_BIN_DIR/pg_isready" -h 127.0.0.1 -p "$LOCAL_PGPORT" >/dev/null 2>&1; do
      i=$((i + 1))
      if [ "$i" -ge 30 ]; then
        echo "[mindweave] ERROR: Embedded PostgreSQL failed to start within 15s" >&2
        exit 1
      fi
      sleep 0.5
    done

    psql -h 127.0.0.1 -p "$LOCAL_PGPORT" -U postgres -d postgres <<'SQL' >/dev/null
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_owner') THEN
    CREATE ROLE mindweave_owner LOGIN PASSWORD 'mindweave_owner' CREATEDB;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_app') THEN
    CREATE ROLE mindweave_app LOGIN PASSWORD 'mindweave_app' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_system') THEN
    CREATE ROLE mindweave_system LOGIN PASSWORD 'mindweave_system' NOSUPERUSER BYPASSRLS;
  END IF;
END
$$;
SELECT 'CREATE DATABASE mindweave OWNER mindweave_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mindweave')\gexec
SQL
    psql -h 127.0.0.1 -p "$LOCAL_PGPORT" -U postgres -d mindweave <<'SQL' >/dev/null
GRANT ALL PRIVILEGES ON DATABASE mindweave TO mindweave_owner;
GRANT USAGE, CREATE ON SCHEMA public TO mindweave_owner;
GRANT USAGE ON SCHEMA public TO mindweave_app, mindweave_system;
SQL
  fi

  export MINDWEAVE_APP_DATABASE_URL="postgresql+asyncpg://mindweave_app:mindweave_app@127.0.0.1:${LOCAL_PGPORT}/mindweave"
  export MINDWEAVE_SYSTEM_DATABASE_URL="postgresql+asyncpg://mindweave_system:mindweave_system@127.0.0.1:${LOCAL_PGPORT}/mindweave"
  export MINDWEAVE_MIGRATION_DATABASE_URL="postgresql+asyncpg://mindweave_owner:mindweave_owner@127.0.0.1:${LOCAL_PGPORT}/mindweave"
  export MINDWEAVE_MIGRATE_ON_BOOT="true"
  export MINDWEAVE_EMBED_WORKER_IN_API="${MINDWEAVE_EMBED_WORKER_IN_API:-true}"
  export MINDWEAVE_CORS_ORIGINS="${MINDWEAVE_CORS_ORIGINS:-[\"mindweave://app\"]}"
fi

# Only expose dev OTP in response when explicitly running dev + noop email provider.
if [ "${MINDWEAVE_ENVIRONMENT:-dev}" = "dev" ] && [ "${MINDWEAVE_EMAIL_PROVIDER:-noop}" = "noop" ]; then
  export MINDWEAVE_EXPOSE_DEV_OTP_IN_RESPONSE="${MINDWEAVE_EXPOSE_DEV_OTP_IN_RESPONSE:-true}"
fi

if [ "${MINDWEAVE_MIGRATE_ON_BOOT:-false}" = "true" ]; then
  echo "[mindweave] Running alembic upgrade head..."
  alembic upgrade head
fi

if [ "${MINDWEAVE_AUTO_SEED_DEMO:-true}" = "true" ] && [ "${MINDWEAVE_ENVIRONMENT:-dev}" = "dev" ] && [ -f "/app/scripts/seed_demo.py" ]; then
  echo "[mindweave] Ensuring demo@mindweave.app showcase workspace is seeded..."
  python /app/scripts/seed_demo.py --email demo@mindweave.app || echo "[mindweave] WARN: demo seed skipped" >&2
fi

if [ "$EMBEDDED_PG_STARTED" = "true" ]; then
  uvicorn mindweave.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log &
  UVICORN_PID=$!
  trap 'kill -TERM "$UVICORN_PID" 2>/dev/null || true; wait "$UVICORN_PID" 2>/dev/null || true; "$PG_BIN_DIR/pg_ctl" -D "$LOCAL_PGDATA" -m fast stop || true; exit 0' TERM INT
  wait "$UVICORN_PID"
else
  exec uvicorn mindweave.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log
fi
