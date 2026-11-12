#!/bin/sh
set -eu

if [ "${MINDWEAVE_MIGRATE_ON_BOOT:-false}" = "true" ]; then
  echo "[mindweave] Running alembic upgrade head..."
  alembic upgrade head
fi

exec uvicorn mindweave.main:app --host 0.0.0.0 --port "${PORT:-8000}"
