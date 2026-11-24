"""Shared pytest fixtures for unit and live PostgreSQL integration tests."""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mindweave.core.clock import ManualClock
from mindweave.core.config import Settings
from mindweave.core.dependencies import AppContainer


def _db_urls() -> tuple[str, str, str, str]:
    db_name = os.environ.get("MINDWEAVE_TEST_DB_NAME", "mindweave_test")
    app_url = f"postgresql+asyncpg://mindweave_app:mindweave_app@127.0.0.1:55432/{db_name}"
    sys_url = f"postgresql+asyncpg://mindweave_system:mindweave_system@127.0.0.1:55432/{db_name}"
    mig_url = f"postgresql+asyncpg://mindweave_owner:mindweave_owner@127.0.0.1:55432/{db_name}"
    return db_name, app_url, sys_url, mig_url


@pytest.fixture(scope="session", autouse=True)
def ensure_postgres() -> None:
    pg_bin = Path("/usr/lib/postgresql/15/bin")
    pg_data = Path("/home/antigravity/.pgdata")
    if not (pg_bin / "pg_isready").exists() or not pg_data.exists():
        return
    ready = subprocess.run(
        [str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", "55432"],
        capture_output=True,
        check=False,
    )
    if ready.returncode != 0:
        subprocess.run(
            [
                str(pg_bin / "pg_ctl"),
                "-D",
                str(pg_data),
                "-l",
                str(pg_data / "pg.log"),
                "-o",
                "-p 55432 -h 127.0.0.1 -k /tmp",
                "start",
            ],
            check=True,
        )


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    _, app_url, sys_url, mig_url = _db_urls()
    return Settings(
        environment="test",
        app_database_url=app_url,
        system_database_url=sys_url,
        migration_database_url=mig_url,
        blob_storage_dir=tmp_path / "blobs",
        ai_primary_provider="fake",
        expose_dev_otp_in_response=True,
        allow_dev_mock_apple_auth=True,
        otp_resend_cooldown_seconds=0,
        trusted_proxy_hops=0,
        otp_ip_rate_limit_per_minute=100,
    )


@pytest.fixture
def _per_db_lock() -> Iterator[None]:
    db_name, _, _, _ = _db_urls()
    lock_file = Path(f"/tmp/mindweave_pg_{db_name}.lock")  # noqa: S108
    with lock_file.open("w") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


@pytest.fixture
async def db_container(
    _per_db_lock: None, test_settings: Settings, manual_clock: ManualClock
) -> AsyncIterator[AppContainer]:
    _, _, _, mig_url = _db_urls()
    container = AppContainer.build(test_settings, clock=manual_clock)
    owner_engine = create_async_engine(mig_url)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE "
                "users, email_otp_challenges, background_jobs, audit_log "
                "RESTART IDENTITY CASCADE"
            )
        )
    await owner_engine.dispose()

    try:
        yield container
    finally:
        await container.db.dispose()
