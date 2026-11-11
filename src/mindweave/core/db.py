"""Async SQLAlchemy 2.0 session management with mandatory PostgreSQL RLS binding (Invariant I1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from mindweave.shared.scopes import UserScope


class Base(DeclarativeBase):
    """Canonical SQLAlchemy 2.0 declarative base."""


class DatabaseManager:
    """Manages separate `app` (NOBYPASSRLS) and `system` (BYPASSRLS) connection pools."""

    def __init__(self, *, app_url: str, system_url: str, echo: bool = False) -> None:
        self._app_engine: AsyncEngine = create_async_engine(
            app_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
        self._system_engine: AsyncEngine = create_async_engine(
            system_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        self._app_factory = async_sessionmaker(
            self._app_engine,
            expire_on_commit=False,
            autoflush=False,
        )
        self._system_factory = async_sessionmaker(
            self._system_engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def app_engine(self) -> AsyncEngine:
        return self._app_engine

    @property
    def system_engine(self) -> AsyncEngine:
        return self._system_engine

    @asynccontextmanager
    async def user_session(self, scope: UserScope) -> AsyncIterator[AsyncSession]:
        """Open a tenant-isolated transaction bound to `app.user_id` via PostgreSQL RLS."""
        async with self._app_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.user_id', :uid, true)"),
                    {"uid": str(scope.user_id)},
                )
                yield session

    @asynccontextmanager
    async def unauthenticated_app_session(self) -> AsyncIterator[AsyncSession]:
        """Open a session on `app_engine` WITHOUT `app.user_id` (used to test RLS blocking)."""
        async with self._app_factory() as session:
            async with session.begin():
                await session.execute(text("SELECT set_config('app.user_id', '', true)"))
                yield session

    @asynccontextmanager
    async def system_session(self) -> AsyncIterator[AsyncSession]:
        """Open a privileged session ONLY for auth bootstrap, worker queue, or cron scan."""
        async with self._system_factory() as session:
            async with session.begin():
                yield session

    async def check_ready(self) -> bool:
        async with self._app_engine.connect() as conn:
            val = await conn.scalar(text("SELECT 1"))
            return bool(val == 1)

    async def dispose(self) -> None:
        await self._app_engine.dispose()
        await self._system_engine.dispose()
