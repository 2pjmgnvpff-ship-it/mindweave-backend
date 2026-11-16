"""Application service assembling the V10 Workspace Bootstrap payload."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mindweave.core.dependencies import AppContainer
from mindweave.modules.bootstrap.domain.formatting import MaterialPreview
from mindweave.modules.bootstrap.infra.query_repo import BootstrapQueryRepository
from mindweave.modules.people.domain.entities import PersonProfile, PhotoSlot
from mindweave.modules.people.domain.tier_transition import partition_person_ids
from mindweave.modules.people.infra.repository import PeopleRepository
from mindweave.shared.enums import CupidLayout, Edition
from mindweave.shared.pencil import QUESTION_INBOX_KINDS
from mindweave.shared.scopes import UserScope


@dataclass(frozen=True, slots=True)
class BootstrapPreferencesSnapshot:
    preferred_edition: Edition
    cupid_layout: CupidLayout
    wallpaper_preset: str
    has_custom_wallpaper: bool
    wallpaper_url: str | None
    wallpaper_blur: int
    wallpaper_vignette: int
    app_lock_enabled: bool
    late_night_care_enabled: bool
    timezone: str
    version: int


@dataclass(frozen=True, slots=True)
class BootstrapPersonAggregate:
    profile: PersonProfile
    avatar_url: str | None
    main_photo_url: str | None
    sub_photo_url: str | None
    materials: list[tuple[MaterialPreview, str | None]]


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapSnapshot:
    preferences: BootstrapPreferencesSnapshot
    core_ids: list[UUID]
    casual_ids: list[UUID]
    people: dict[str, BootstrapPersonAggregate]
    inbox_count: int
    question_count: int = 0


class BootstrapService:
    """Aggregates preferences, ordered core/casual IDs, hydrated `PEOPLE_DB`, and inbox count."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._people_repo = PeopleRepository(container.cipher)
        self._query_repo = BootstrapQueryRepository(container.cipher)

    def _photo_url(
        self,
        scope: UserScope,
        person_id: UUID,
        slot: PhotoSlot,
        object_key: str | None,
        *,
        now: datetime,
    ) -> str | None:
        if not object_key:
            return None
        prefix = self._container.settings.api_prefix.rstrip("/")
        raw_path = f"{prefix}/persons/{person_id}/photos/{slot.value}"
        return self._container.tokens.sign_media_path(
            raw_path,
            user_id=scope.user_id,
            now=now,
        )

    def _material_media_url(
        self,
        scope: UserScope,
        record_id: UUID,
        media_object_key: str | None,
        *,
        now: datetime,
    ) -> str | None:
        if not media_object_key:
            return None
        prefix = self._container.settings.api_prefix.rstrip("/")
        raw_path = f"{prefix}/materials/{record_id}/media"
        return self._container.tokens.sign_media_path(
            raw_path,
            user_id=scope.user_id,
            now=now,
        )

    async def load_workspace(self, scope: UserScope) -> WorkspaceBootstrapSnapshot:
        prefix = self._container.settings.api_prefix.rstrip("/")
        async with self._container.db.user_session(scope) as session:
            dek = await self._container.load_user_dek(session, scope)
            now = self._container.clock.now()

            pref_row = await self._query_repo.get_or_provision_preferences(
                session,
                scope,
                default_timezone=self._container.settings.default_timezone,
                now=now,
            )
            has_wallpaper = bool(pref_row.wallpaper_object_key)
            signed_wallpaper_url = (
                self._container.tokens.sign_media_path(
                    f"{prefix}/me/wallpaper",
                    user_id=scope.user_id,
                    now=now,
                )
                if has_wallpaper
                else None
            )
            prefs = BootstrapPreferencesSnapshot(
                preferred_edition=Edition(pref_row.preferred_edition),
                cupid_layout=CupidLayout(pref_row.cupid_layout),
                wallpaper_preset=pref_row.wallpaper_preset,
                has_custom_wallpaper=has_wallpaper,
                wallpaper_url=signed_wallpaper_url,
                wallpaper_blur=pref_row.wallpaper_blur,
                wallpaper_vignette=pref_row.wallpaper_vignette,
                app_lock_enabled=pref_row.app_lock_enabled,
                late_night_care_enabled=pref_row.late_night_care_enabled,
                timezone=pref_row.timezone,
                version=pref_row.version,
            )

            inbox_count = await self._query_repo.count_open_inbox_items(session, scope)
            question_count = await self._query_repo.count_open_inbox_items(
                session, scope, kinds=QUESTION_INBOX_KINDS
            )
            person_rows = await self._query_repo.list_workspace_person_rows(session, scope)
            if not person_rows:
                return WorkspaceBootstrapSnapshot(
                    preferences=prefs,
                    core_ids=[],
                    casual_ids=[],
                    people={},
                    inbox_count=inbox_count,
                    question_count=question_count,
                )

            rank_entries = [self._people_repo.to_rank_entry(r) for r in person_rows]
            partition = partition_person_ids(rank_entries, include_provisional=True)
            ordered_ids = [*partition.core_ids, *partition.casual_ids]
            row_map = {r.id: r for r in person_rows}

            materials_by_pid = await self._query_repo.load_top_materials_by_person(
                session, scope, dek, ordered_ids
            )

            people_dict: dict[str, BootstrapPersonAggregate] = {}
            for pid in ordered_ids:
                row = row_map.get(pid)
                if row is None:
                    continue
                profile = self._people_repo.decrypt_person_row(row, scope=scope, dek=dek)
                mat_items = [
                    (
                        m,
                        self._material_media_url(scope, m.id, m.media_object_key, now=now),
                    )
                    for m in materials_by_pid.get(pid, ())
                ]
                people_dict[str(pid)] = BootstrapPersonAggregate(
                    profile=profile,
                    avatar_url=self._photo_url(
                        scope, pid, PhotoSlot.AVATAR, profile.avatar_object_key, now=now
                    ),
                    main_photo_url=self._photo_url(
                        scope, pid, PhotoSlot.MAIN, profile.main_photo_object_key, now=now
                    ),
                    sub_photo_url=self._photo_url(
                        scope, pid, PhotoSlot.SUB, profile.sub_photo_object_key, now=now
                    ),
                    materials=mat_items,
                )

            return WorkspaceBootstrapSnapshot(
                preferences=prefs,
                core_ids=partition.core_ids,
                casual_ids=partition.casual_ids,
                people=people_dict,
                inbox_count=inbox_count,
                question_count=question_count,
            )
