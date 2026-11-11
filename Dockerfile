# syntax=docker/dockerfile:1.7
# Multi-stage non-root Python 3.12 container built with `uv`

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
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY evals ./evals
COPY prompts ./prompts
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ----------------------------------------------------------------------------
# Stage 2: Minimal non-root runtime image
# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    MINDWEAVE_BLOB_STORAGE_DIR="/var/lib/mindweave/blobs"

RUN groupadd --gid 10001 mindweave \
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

USER mindweave:mindweave

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "mindweave.main:app", "--host", "0.0.0.0", "--port", "8000"]
