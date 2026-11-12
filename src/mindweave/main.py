"""MindWeave (心络) FastAPI Application Factory & Unified Composition Root."""

from __future__ import annotations

import logging
import mimetypes
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import text

from mindweave.core.config import Settings, get_settings
from mindweave.core.dependencies import AppContainer, get_container
from mindweave.core.dto import CamelModel
from mindweave.core.errors import AppError, problem_response, register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.logging import configure_logging, get_logger
from mindweave.modules.bootstrap.api.router import router as bootstrap_router
from mindweave.modules.capture.api.router import router as capture_router
from mindweave.modules.companion.api.router import router as companion_router
from mindweave.modules.data_rights.api.router import router as data_rights_router
from mindweave.modules.identity.api.router import router as identity_router
from mindweave.modules.memory.api.router import router as memory_router
from mindweave.modules.people.api.router import router as people_router
from mindweave.modules.preferences.api.router import router as preferences_router
from mindweave.modules.reminders.api.router import router as reminders_router
from mindweave.modules.telemetry.api.router import router as telemetry_router
from mindweave.wiring import wire_container_services

logger = get_logger(__name__)

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/webp", ".webp")

_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_ALL_MODULE_ROUTERS = (
    identity_router,
    preferences_router,
    bootstrap_router,
    people_router,
    capture_router,
    memory_router,
    companion_router,
    reminders_router,
    data_rights_router,
    telemetry_router,
)


class HealthStatusResponse(CamelModel):
    status: str = Field(default="ok")
    service: str = Field(default="mindweave-backend")
    version: str = Field(default="0.1.0")


class ReadinessStatusResponse(CamelModel):
    status: str = Field(default="ready")
    database: str = Field(default="ok")


def _resolve_request_id(raw_header: str | None) -> str:
    if raw_header:
        candidate = raw_header.strip()
        if _SAFE_REQUEST_ID_RE.match(candidate):
            return candidate
    return str(new_id())


def create_app(
    *,
    settings: Settings | None = None,
    container: AppContainer | None = None,
) -> FastAPI:
    """Construct and wire the complete MindWeave FastAPI application."""
    resolved_settings = (
        container.settings if container is not None else (settings or get_settings())
    )
    configure_logging(
        json_logs=resolved_settings.environment not in ("dev", "test"),
        level=logging.DEBUG if resolved_settings.debug else logging.INFO,
    )

    owns_container = container is None
    app_container = container or AppContainer.build(resolved_settings)
    wire_container_services(app_container)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        import asyncio

        app_instance.state.container = app_container
        wire_container_services(app_container)
        worker_task: asyncio.Task[None] | None = None
        if resolved_settings.embed_worker_in_api:
            from mindweave.workers.__main__ import build_default_worker_runner

            embedded_runner = build_default_worker_runner(app_container)
            worker_task = asyncio.create_task(
                embedded_runner.run_forever(poll_interval=0.5),
                name="mindweave-embedded-worker",
            )
        logger.info(
            "app.startup",
            service=resolved_settings.service_name,
            environment=resolved_settings.environment,
            ai_primary_provider=resolved_settings.ai_primary_provider,
            embed_worker_in_api=resolved_settings.embed_worker_in_api,
        )
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            if owns_container:
                await app_container.db.dispose()
            logger.info("app.shutdown", service=resolved_settings.service_name)

    app = FastAPI(
        title="MindWeave (心络) Backend API",
        description=(
            "Privacy-first relationship memory, multimodal capture, and Cupid companion API. "
            "Enforces PostgreSQL RLS, per-user AES-256-GCM envelope encryption, GatePolicy, "
            "and independent L1+L2 safety guardrails."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.container = app_container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "ETag"],
    )

    @app.middleware("http")
    async def request_context_and_zero_pii_access_log(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = _resolve_request_id(request.headers.get("X-Request-Id"))
        request.state.request_id = req_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=req_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except AppError as exc:
            # Ensure AppErrors caught inside middleware chains still render RFC 7807 + X-Request-Id
            response = problem_response(
                status_code=exc.status_code,
                error_code=exc.error_code,
                title=exc.title,
                detail=exc.detail,
                instance=str(request.url.path),
                request_id=req_id,
                extensions=exc.extensions,
            )
        except Exception as exc:
            logger.error(
                "http.unhandled_exception",
                method=request.method,
                path=request.url.path,
                error_type=type(exc).__name__,
            )
            response = problem_response(
                status_code=500,
                error_code="internal_error",
                title="Internal Server Error",
                detail="An unexpected internal error occurred.",
                instance=str(request.url.path),
                request_id=req_id,
            )
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

        response.headers["X-Request-Id"] = req_id
        logger.info(
            "http.access",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=elapsed_ms,
        )
        structlog.contextvars.clear_contextvars()
        return response

    register_exception_handlers(app)

    @app.get("/healthz", response_model=HealthStatusResponse, tags=["ops"])
    async def healthz() -> HealthStatusResponse:
        return HealthStatusResponse(
            status="ok",
            service=resolved_settings.service_name,
            version="0.1.0",
        )

    @app.get("/readyz", response_model=ReadinessStatusResponse, tags=["ops"])
    async def readyz(request: Request) -> ReadinessStatusResponse | JSONResponse:
        active_container = get_container(request)
        try:
            async with active_container.db.system_session() as sys_sess:
                probe = await sys_sess.scalar(text("SELECT 1"))
            if probe != 1:
                raise RuntimeError("Unexpected database probe result")
        except Exception as exc:
            logger.error("ops.readyz_failed", error_type=type(exc).__name__)
            req_id = getattr(request.state, "request_id", None)
            return problem_response(
                status_code=503,
                error_code="database_unavailable",
                title="Service Unavailable",
                detail="Database readiness probe failed.",
                instance=str(request.url.path),
                request_id=req_id,
            )
        return ReadinessStatusResponse(status="ready", database="ok")

    webapp_dir = Path(__file__).resolve().parent / "webapp"
    if webapp_dir.is_dir():
        for subdir_name in ("assets", "styles", "scripts"):
            subdir = webapp_dir / subdir_name
            if subdir.is_dir():
                app.mount(
                    f"/{subdir_name}",
                    StaticFiles(directory=str(subdir)),
                    name=f"webapp-{subdir_name}",
                )

        webapp_index_path = webapp_dir / "index.html"

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def production_webapp_root() -> HTMLResponse:
            return HTMLResponse(
                content=webapp_index_path.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-cache"},
            )

    if resolved_settings.environment in ("dev", "test"):
        workbench_path = Path(__file__).resolve().parent / "tools" / "workbench.html"

        @app.get("/workbench", response_class=HTMLResponse, include_in_schema=False)
        async def dev_workbench() -> HTMLResponse:
            return HTMLResponse(content=workbench_path.read_text(encoding="utf-8"))

    canonical_prefix = resolved_settings.api_prefix.rstrip("/") or "/v1"
    for module_router in _ALL_MODULE_ROUTERS:
        app.include_router(module_router, prefix=canonical_prefix)
        # V10 frontend compatibility alias (`/api/v1/...`), omitted from canonical OpenAPI snapshot
        if canonical_prefix != "/api/v1":
            app.include_router(module_router, prefix="/api/v1", include_in_schema=False)

    return app


app = create_app()
