"""CamelCase request and response DTOs for User Preferences endpoints."""

from __future__ import annotations

from pydantic import Field, StrictBool, StrictInt, field_validator

from mindweave.core.dto import CamelModel
from mindweave.modules.preferences.domain.models import (
    MAX_WALLPAPER_BLUR,
    MAX_WALLPAPER_VIGNETTE,
    MIN_WALLPAPER_BLUR,
    MIN_WALLPAPER_VIGNETTE,
    PreferencesPatch,
    PreferencesValidationError,
    UserPreferences,
    validate_timezone,
    validate_wallpaper_preset,
)
from mindweave.shared.enums import CupidLayout, Edition


class PreferencesResponse(CamelModel):
    preferred_edition: Edition
    cupid_layout: CupidLayout
    wallpaper_preset: str
    has_custom_wallpaper: bool
    wallpaper_url: str | None = None
    wallpaper_blur: int = Field(ge=MIN_WALLPAPER_BLUR, le=MAX_WALLPAPER_BLUR)
    wallpaper_vignette: int = Field(ge=MIN_WALLPAPER_VIGNETTE, le=MAX_WALLPAPER_VIGNETTE)
    app_lock_enabled: bool
    late_night_care_enabled: bool
    timezone: str
    version: int = Field(ge=1)

    @classmethod
    def from_domain(
        cls,
        prefs: UserPreferences,
        *,
        wallpaper_url: str | None = None,
    ) -> PreferencesResponse:
        return cls(
            preferred_edition=prefs.preferred_edition,
            cupid_layout=prefs.cupid_layout,
            wallpaper_preset=prefs.wallpaper_preset,
            has_custom_wallpaper=prefs.has_custom_wallpaper,
            wallpaper_url=wallpaper_url if prefs.has_custom_wallpaper else None,
            wallpaper_blur=prefs.wallpaper_blur,
            wallpaper_vignette=prefs.wallpaper_vignette,
            app_lock_enabled=prefs.app_lock_enabled,
            late_night_care_enabled=prefs.late_night_care_enabled,
            timezone=prefs.timezone,
            version=prefs.version,
        )


class UpdatePreferencesRequest(CamelModel):
    preferred_edition: Edition | None = None
    cupid_layout: CupidLayout | None = None
    wallpaper_preset: str | None = Field(default=None, min_length=1, max_length=48)
    wallpaper_blur: StrictInt | None = Field(
        default=None, ge=MIN_WALLPAPER_BLUR, le=MAX_WALLPAPER_BLUR
    )
    wallpaper_vignette: StrictInt | None = Field(
        default=None, ge=MIN_WALLPAPER_VIGNETTE, le=MAX_WALLPAPER_VIGNETTE
    )
    app_lock_enabled: StrictBool | None = None
    late_night_care_enabled: StrictBool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("wallpaper_preset")
    @classmethod
    def _check_preset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_wallpaper_preset(value)
        except PreferencesValidationError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_timezone(value)
        except PreferencesValidationError as exc:
            raise ValueError(str(exc)) from exc

    def to_domain_patch(self) -> PreferencesPatch:
        return PreferencesPatch(
            preferred_edition=self.preferred_edition,
            cupid_layout=self.cupid_layout,
            wallpaper_preset=self.wallpaper_preset,
            wallpaper_blur=self.wallpaper_blur,
            wallpaper_vignette=self.wallpaper_vignette,
            app_lock_enabled=self.app_lock_enabled,
            late_night_care_enabled=self.late_night_care_enabled,
            timezone=self.timezone,
        )
