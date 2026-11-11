"""Shared pytest fixtures for unit and live PostgreSQL integration tests."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from mindweave.core.clock import ManualClock
from mindweave.core.config import Settings
from mindweave.core.dependencies import AppContainer

TEST_APP_DB_URL = "postgresql+asyncpg://mindweave_app:mindweave_app@127.0.0.1:55432/mindweave_test"
TEST_SYSTEM_DB_URL = (
    "postgresql+asyncpg://mindweave_system:mindweave_system@127.0.0.1:55432/mindweave_test"
)
TEST_MIGRATION_DB_URL = (
    "postgresql+asyncpg://mindweave_owner:mindweave_owner@127.0.0.1:55432/mindweave_test"
)


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
    return Settings(
        environment="test",
        app_database_url=TEST_APP_DB_URL,
        system_database_url=TEST_SYSTEM_DB_URL,
        migration_database_url=TEST_MIGRATION_DB_URL,
        blob_storage_dir=tmp_path / "blobs",
        ai_primary_provider="fake",
        expose_dev_otp_in_response=True,
        allow_dev_mock_apple_auth=True,
    )


@pytest.fixture
async def db_container(
    test_settings: Settings, manual_clock: ManualClock
) -> AsyncIterator[AppContainer]:
    container = AppContainer.build(test_settings, clock=manual_clock)
    # Clean all test tables cleanly via system_session before test
    from sqlalchemy.ext.asyncio import create_async_engine

    owner_engine = create_async_engine(TEST_MIGRATION_DB_URL)
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
