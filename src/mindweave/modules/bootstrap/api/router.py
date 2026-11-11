"""FastAPI router for `GET /bootstrap` (V10 Workspace Aggregation)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from mindweave.core.dependencies import AppContainer, get_container, get_current_user_scope
from mindweave.modules.bootstrap.api.schemas import (
    BootstrapMaterialPreviewDTO,
    BootstrapPersonDTO,
    BootstrapPreferencesDTO,
    BootstrapResponse,
)
from mindweave.modules.bootstrap.application.service import BootstrapService
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["bootstrap"])


@router.get(
    "/bootstrap",
    response_model=BootstrapResponse,
    response_model_by_alias=True,
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
                )
                for mat, media_url in agg.materials
            ],
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
