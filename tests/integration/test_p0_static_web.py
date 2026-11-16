"""P0 0.9 same-origin PWA hosting at `/`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mindweave.core.config import Settings
from mindweave.core.dependencies import AppContainer
from mindweave.main import create_app

# ---------------------------------------------------------------------------
# 0.9 PWA served at `/`
# ---------------------------------------------------------------------------


def _make_web_dir(tmp_path: Path) -> Path:
    web = tmp_path / "web"
    (web / "_v10_reference").mkdir(parents=True)
    (web / "tests").mkdir()
    (web / "scripts").mkdir()
    (web / "index.html").write_text("<!doctype html><title>心络</title>", encoding="utf-8")
    (web / "sw.js").write_text("self.addEventListener('fetch', () => {});", encoding="utf-8")
    (web / "scripts" / "app.js").write_text("export const ok = true;", encoding="utf-8")
    (web / "_v10_reference" / "index.html").write_text("ref", encoding="utf-8")
    (web / "tests" / "index.html").write_text("tests", encoding="utf-8")
    (web / ".env").write_text("SECRET=1", encoding="utf-8")
    return web


def _app_for(test_settings: Settings, environment: str, web_dir: Path) -> FastAPI:
    overrides: dict[str, object] = {"environment": environment, "web_dist_dir": web_dir}
    if environment in ("staging", "prod_cn"):
        overrides.update(
            expose_dev_otp_in_response=False,
            allow_dev_mock_apple_auth=False,
            push_apns_adapter="disabled",
            push_web_push_adapter="disabled",
        )
    settings = test_settings.model_copy(update=overrides)
    return create_app(container=AppContainer.build(settings))


@pytest.mark.asyncio
async def test_pwa_served_at_root_with_security_headers_in_dev(
    test_settings: Settings, tmp_path: Path
) -> None:
    app = _app_for(test_settings, "dev", _make_web_dir(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert "心络" in root.text
        csp = root.headers["content-security-policy"]
        assert "connect-src 'self'" in csp
        assert "img-src 'self' blob: data:" in csp
        assert "media-src 'self' blob: data:" in csp
        assert root.headers["permissions-policy"].startswith("microphone=(self)")
        assert root.headers["x-content-type-options"] == "nosniff"
        assert root.headers["cache-control"] == "no-cache"

        sw = await client.get("/sw.js")
        assert sw.status_code == 200
        assert sw.headers["cache-control"] == "no-cache"
        assert (await client.get("/scripts/app.js")).status_code == 200

        # dev: design reference + workbench reachable; dotfiles never
        assert (await client.get("/_v10_reference/")).status_code == 200
        assert (await client.get("/dev/workbench")).status_code == 200
        assert (await client.get("/workbench")).status_code == 404
        assert (await client.get("/.env")).status_code == 404

        # API & ops routes keep priority over the static mount
        assert (await client.get("/healthz")).json()["status"] == "ok"
        assert (await client.get("/v1/bootstrap")).status_code == 401
        assert (await client.get("/api/v1/bootstrap")).status_code == 401
        assert (await client.get("/docs")).status_code == 200
        assert "content-security-policy" not in (await client.get("/healthz")).headers


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "prod_cn"])
async def test_pwa_hides_design_reference_and_workbench_outside_dev(
    test_settings: Settings, tmp_path: Path, environment: str
) -> None:
    overrides: dict[str, object] = {}
    if environment == "prod_cn":
        overrides = {
            "ai_primary_provider": "deepseek",
            "kek_hex": "ab" * 32,
            "jwt_secret": "y" * 48,
        }
    base = test_settings.model_copy(update=overrides)
    app = _app_for(base, environment, _make_web_dir(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/_v10_reference/")).status_code == 404
        assert (await client.get("/_v10_reference/index.html")).status_code == 404
        assert (await client.get("/tests/index.html")).status_code == 404
        assert (await client.get("/dev/workbench")).status_code == 404
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_missing_web_dir_does_not_break_api(test_settings: Settings, tmp_path: Path) -> None:
    app = _app_for(test_settings, "dev", tmp_path / "does-not-exist")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/healthz")).status_code == 200
