"""SQLAlchemy repository for RLS-protected `user_preferences` rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.orm import UserPreferenceRow
from mindweave.modules.preferences.domain.models import (
    UserPreferences,
    default_user_preferences,
)
from mindweave.shared.enums import CupidLayout, Edition
from mindweave.shared.scopes import UserScope


class PreferencesRepository:
    """Maps `UserPreferenceRow` inside RLS-bound `user_session(scope)`."""

    async def get_optional(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        for_update: bool = False,
    ) -> UserPreferences | None:
        stmt = select(UserPreferenceRow).where(UserPreferenceRow.user_id == scope.user_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = await session.scalar(stmt)
        if row is None:
            return None
        return self._to_domain(row)

    async def get_or_create(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        default_timezone: str,
        now: datetime,
        for_update: bool = False,
    ) -> UserPreferences:
        existing = await self.get_optional(session, scope, for_update=for_update)
        if existing is not None:
            return existing

        defaults = default_user_preferences(
            user_id=scope.user_id,
            timezone=default_timezone,
            now=now,
        )
        row = UserPreferenceRow(
            user_id=scope.user_id,
            preferred_edition=defaults.preferred_edition.value,
            cupid_layout=defaults.cupid_layout.value,
            wallpaper_preset=defaults.wallpaper_preset,
            wallpaper_object_key=defaults.wallpaper_object_key,
            wallpaper_blur=defaults.wallpaper_blur,
            wallpaper_vignette=defaults.wallpaper_vignette,
            app_lock_enabled=defaults.app_lock_enabled,
            late_night_care_enabled=defaults.late_night_care_enabled,
            timezone=defaults.timezone,
            version=defaults.version,
            updated_at=defaults.updated_at,
        )
        session.add(row)
        await session.flush()
        return self._to_domain(row)

    async def save(
        self,
        session: AsyncSession,
        scope: UserScope,
        prefs: UserPreferences,
    ) -> UserPreferences:
        stmt = (
            select(UserPreferenceRow)
            .where(UserPreferenceRow.user_id == scope.user_id)
            .with_for_update()
        )
        row = await session.scalar(stmt)
        if row is None:
            row = UserPreferenceRow(user_id=scope.user_id)
            session.add(row)

        row.preferred_edition = prefs.preferred_edition.value
        row.cupid_layout = prefs.cupid_layout.value
        row.wallpaper_preset = prefs.wallpaper_preset
        row.wallpaper_object_key = prefs.wallpaper_object_key
        row.wallpaper_blur = prefs.wallpaper_blur
        row.wallpaper_vignette = prefs.wallpaper_vignette
        row.app_lock_enabled = prefs.app_lock_enabled
        row.late_night_care_enabled = prefs.late_night_care_enabled
        row.timezone = prefs.timezone
        row.version = prefs.version
        row.updated_at = prefs.updated_at
        await session.flush()
        return self._to_domain(row)

    @staticmethod
    def _to_domain(row: UserPreferenceRow) -> UserPreferences:
        return UserPreferences(
            user_id=row.user_id,
            preferred_edition=Edition(row.preferred_edition),
            cupid_layout=CupidLayout(row.cupid_layout),
            wallpaper_preset=row.wallpaper_preset,
            wallpaper_object_key=row.wallpaper_object_key,
            wallpaper_blur=row.wallpaper_blur,
            wallpaper_vignette=row.wallpaper_vignette,
            app_lock_enabled=row.app_lock_enabled,
            late_night_care_enabled=row.late_night_care_enabled,
            timezone=row.timezone,
            version=row.version,
            updated_at=row.updated_at,
        )
