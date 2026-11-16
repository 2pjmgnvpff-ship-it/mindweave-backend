"""Same-origin static hosting for the MindWeave PWA (`web/`) with hardened security headers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import anyio
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# `connect-src 'self'` keeps every API / SSE call same-origin. Images & media additionally allow
# `blob:` / `data:` so locally recorded audio and optimistic photo previews can render.
STATIC_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' blob: data:",
        "media-src 'self' blob: data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

STATIC_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": STATIC_CONTENT_SECURITY_POLICY,
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=(), payment=()",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Entry documents must always be revalidated so a new deploy is picked up immediately.
_NO_CACHE_FILENAMES = frozenset({"sw.js", "service-worker.js", "manifest.webmanifest"})

# Design references & frontend test harnesses are never product surface outside dev/test.
NON_PRODUCT_WEB_PREFIXES: tuple[str, ...] = ("_v10_reference", "tests")


class ProductStaticFiles(StaticFiles):
    """`StaticFiles` that hides dotfiles / blocked prefixes and stamps security headers."""

    def __init__(self, *, directory: Path, blocked_prefixes: Iterable[str] = ()) -> None:
        super().__init__(directory=directory, html=True, check_dir=False)
        self._blocked_prefixes = frozenset(p.strip("/") for p in blocked_prefixes if p.strip("/"))

    def _is_blocked(self, path: str) -> bool:
        segments = [seg for seg in path.replace("\\", "/").split("/") if seg and seg != "."]
        if any(seg.startswith(".") for seg in segments):
            return True
        return bool(segments) and segments[0] in self._blocked_prefixes

    async def check_config(self) -> None:
        # A missing `web/` (e.g. API-only deployments, early dev) must 404, never 500.
        if self.directory is not None and not await anyio.Path(self.directory).is_dir():
            return
        await super().check_config()

    async def get_response(self, path: str, scope: Scope) -> Response:
        if self._is_blocked(path):
            raise HTTPException(status_code=404)
        response = await super().get_response(path, scope)
        for header, value in STATIC_SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        filename = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        is_html = response.headers.get("content-type", "").startswith("text/html")
        if is_html or filename in _NO_CACHE_FILENAMES:
            response.headers["Cache-Control"] = "no-cache"
        return response


def build_product_static_app(*, web_dir: Path, environment: str) -> ProductStaticFiles:
    """Build the `/` static app; non-product folders are only exposed in dev/test."""
    blocked = () if environment in ("dev", "test") else NON_PRODUCT_WEB_PREFIXES
    return ProductStaticFiles(directory=web_dir, blocked_prefixes=blocked)
