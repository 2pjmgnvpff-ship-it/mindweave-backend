"""FastAPI router for `GET /bootstrap` (V10 Workspace Aggregation)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from mindweave.core.dependencies import AppContainer, get_container, get_current_user_scope
from mindweave.modules.bootstrap.api.schemas import (
    BootstrapFactPreviewDTO,
    BootstrapMaterialPreviewDTO,
    BootstrapPersonDTO,
    BootstrapPreferencesDTO,
    BootstrapReminderPreviewDTO,
    BootstrapResponse,
)
from mindweave.modules.bootstrap.application.service import BootstrapService
from mindweave.modules.bootstrap.domain.formatting import resolve_visual_token_colors
from mindweave.shared.enums import RecordKind
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["bootstrap"])


@router.get(
    "/bootstrap",
    response_model=BootstrapResponse,
    response_model_by_alias=True,
    response_model_exclude_none=False,
)
async def get_workspace_bootstrap(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> BootstrapResponse:
    service = BootstrapService(container)
    snap = await service.load_workspace(scope)

    people_dtos: dict[str, BootstrapPersonDTO] = {}
    for key, agg in snap.people.items():
        p = agg.profile
        colors = resolve_visual_token_colors(p.tier, p.visual_tokens)
        people_dtos[key] = BootstrapPersonDTO(
            id=str(p.id),
            name_cn=p.name_cn,
            monogram=p.monogram,
            role_tag=p.role_tag,
            context_sub=p.context_sub,
            cupid_title=p.cupid_title,
            tier=p.tier,
            state=p.state,
            has_photo=p.has_photo,
            short_memo=p.short_memo,
            traits=p.traits,
            visual_tokens=p.visual_tokens,
            seal_bg=colors.seal_bg,
            seal_color=colors.seal_color,
            tape_color=colors.tape_color,
            tab_color=colors.tab_color,
            avatar=agg.avatar_url,
            main_photo=agg.main_photo_url,
            sub_photo=agg.sub_photo_url,
            avatar_url=agg.avatar_url,
            main_photo_url=agg.main_photo_url,
            sub_photo_url=agg.sub_photo_url,
            materials=[
                BootstrapMaterialPreviewDTO(
                    id=mat.id,
                    type=mat.kind,
                    title=mat.title,
                    body=mat.body,
                    snippet=mat.snippet,
                    dur=mat.dur,
                    stamp_code=mat.stamp_code,
                    happened_on=mat.happened_on,
                    media_url=media_url,
                    thumb=media_url if mat.kind is RecordKind.PHOTO else None,
                    is_favorite=mat.is_favorite,
                    card_meta=mat.card_meta,
                    review_state=mat.review_state,
                    version=mat.version,
                )
                for mat, media_url in agg.materials
            ],
            facts=[
                BootstrapFactPreviewDTO(
                    id=f.id,
                    record_id=f.record_id,
                    type=f.type,
                    polarity=f.polarity,
                    content=f.content,
                    status=f.status,
                    trigger_date=f.trigger_date,
                    is_lunar=f.is_lunar,
                    lunar_month=f.lunar_month,
                    lunar_day=f.lunar_day,
                    lunar_is_leap_month=f.lunar_is_leap_month,
                    version=f.version,
                )
                for f in agg.facts
            ],
            upcoming_reminders=[
                BootstrapReminderPreviewDTO(
                    id=rem.id,
                    fact_id=rem.fact_id,
                    title=rem.title,
                    rule=rem.rule,
                    is_lunar=rem.is_lunar,
                    target_month=rem.target_month,
                    target_day=rem.target_day,
                    is_leap_month=rem.is_leap_month,
                    once_date=rem.once_date,
                    lead_days=rem.lead_days,
                    next_fire_at=rem.next_fire_at,
                    status=rem.status,
                    version=rem.version,
                )
                for rem in agg.upcoming_reminders
            ],
            cooling_count=agg.cooling_count,
            version=p.version,
        )

    return BootstrapResponse(
        preferences=BootstrapPreferencesDTO(
            edition=snap.preferences.preferred_edition,
            preferred_edition=snap.preferences.preferred_edition,
            cupid_layout=snap.preferences.cupid_layout,
            wallpaper_preset=snap.preferences.wallpaper_preset,
            has_custom_wallpaper=snap.preferences.has_custom_wallpaper,
            wallpaper_url=snap.preferences.wallpaper_url,
            wallpaper_blur=snap.preferences.wallpaper_blur,
            wallpaper_vignette=snap.preferences.wallpaper_vignette,
            app_lock_enabled=snap.preferences.app_lock_enabled,
            late_night_care_enabled=snap.preferences.late_night_care_enabled,
            timezone=snap.preferences.timezone,
            version=snap.preferences.version,
        ),
        core_ids=[str(pid) for pid in snap.core_ids],
        casual_ids=[str(pid) for pid in snap.casual_ids],
        people=people_dtos,
        inbox_count=snap.inbox_count,
    )
