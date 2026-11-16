# Multi-stage non-root Python 3.12 container built with `uv` (Google Cloud Run & Docker ready)

FROM ghcr.io/astral-sh/uv:0.5.14 AS uv_bin

# ----------------------------------------------------------------------------
# Stage 1: Dependency & wheel builder
# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv_bin /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY evals ./evals
COPY prompts ./prompts
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN uv sync --frozen --no-dev

# ----------------------------------------------------------------------------
# Stage 2: Non-root runtime image (includes PostgreSQL 15 for zero-config Cloud Run demo)
# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    MINDWEAVE_BLOB_STORAGE_DIR="/tmp/mindweave-blobs" \
    MINDWEAVE_WEB_DIST_DIR="/app/web"

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-15 postgresql-client-15 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 mindweave \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mindweave \
    && mkdir -p /var/lib/mindweave/blobs \
    && chown -R mindweave:mindweave /var/lib/mindweave

WORKDIR /app

COPY --from=builder --chown=mindweave:mindweave /app/.venv /app/.venv
COPY --from=builder --chown=mindweave:mindweave /app/src /app/src
COPY --from=builder --chown=mindweave:mindweave /app/evals /app/evals
COPY --from=builder --chown=mindweave:mindweave /app/prompts /app/prompts
COPY --from=builder --chown=mindweave:mindweave /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=mindweave:mindweave /app/migrations /app/migrations
# Product PWA served same-origin at `/` (`web/_v10_reference` & `web/tests` are excluded via .dockerignore)
COPY --chown=mindweave:mindweave web /app/web
COPY --chown=mindweave:mindweave scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN chmod +x /app/scripts/entrypoint.sh

USER mindweave:mindweave

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3)"

CMD ["/app/scripts/entrypoint.sh"]
