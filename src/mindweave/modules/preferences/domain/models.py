"""Pure domain entities, value objects, and invariants for User Preferences.

Strictly zero framework, database, HTTP, Pillow, or ORM imports (enforced by `.importlinter`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mindweave.shared.enums import CupidLayout, Edition

MIN_WALLPAPER_BLUR = 0
MAX_WALLPAPER_BLUR = 48
MIN_WALLPAPER_VIGNETTE = 0
MAX_WALLPAPER_VIGNETTE = 85

DEFAULT_EDITION = Edition.COTTON
DEFAULT_CUPID_LAYOUT = CupidLayout.A
DEFAULT_WALLPAPER_PRESET = "warm_paper"
DEFAULT_WALLPAPER_BLUR = 12
DEFAULT_WALLPAPER_VIGNETTE = 20


class PreferencesDomainError(ValueError):
    """Base pure-domain error for the preferences module."""


class PreferencesValidationError(PreferencesDomainError):
    """Raised when preference field invariants (blur, vignette, timezone, etc.) fail."""


class PreferencesVersionConflictError(PreferencesDomainError):
    """Raised when If-Match optimistic locking version mismatches current entity version."""

    def __init__(self, *, expected_version: int, current_version: int) -> None:
        super().__init__(
            f"Optimistic lock mismatch: expected version {expected_version}, "
            f"current version is {current_version}"
        )
        self.expected_version = expected_version
        self.current_version = current_version


def validate_edition(value: Edition | str) -> Edition:
    if isinstance(value, Edition):
        return value
    try:
        return Edition(value.strip().lower())
    except (ValueError, AttributeError) as exc:
        raise PreferencesValidationError("Invalid preferredEdition value") from exc


def validate_cupid_layout(value: CupidLayout | str) -> CupidLayout:
    if isinstance(value, CupidLayout):
        return value
    try:
        return CupidLayout(value.strip().lower())
    except (ValueError, AttributeError) as exc:
        raise PreferencesValidationError("Invalid cupidLayout value") from exc


def validate_wallpaper_preset(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 48 or any(ord(ch) < 32 for ch in cleaned):
        raise PreferencesValidationError("wallpaperPreset must be 1..48 printable characters")
    return cleaned


def validate_wallpaper_blur(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreferencesValidationError("wallpaperBlur must be an integer")
    if value < MIN_WALLPAPER_BLUR or value > MAX_WALLPAPER_BLUR:
        raise PreferencesValidationError(
            f"wallpaperBlur must be between {MIN_WALLPAPER_BLUR} and {MAX_WALLPAPER_BLUR}"
        )
    return value


def validate_wallpaper_vignette(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreferencesValidationError("wallpaperVignette must be an integer")
    if value < MIN_WALLPAPER_VIGNETTE or value > MAX_WALLPAPER_VIGNETTE:
        raise PreferencesValidationError(
            f"wallpaperVignette must be between {MIN_WALLPAPER_VIGNETTE} and {MAX_WALLPAPER_VIGNETTE}"
        )
    return value


def validate_timezone(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 64 or any(ch.isspace() for ch in cleaned):
        raise PreferencesValidationError("timezone must be a valid IANA timezone identifier")
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise PreferencesValidationError(
            "timezone must be a valid IANA timezone identifier"
        ) from exc
    return cleaned


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """User visual & companion care preferences entity."""

    user_id: UUID
    preferred_edition: Edition
    cupid_layout: CupidLayout
    wallpaper_preset: str
    wallpaper_object_key: str | None
    wallpaper_blur: int
    wallpaper_vignette: int
    app_lock_enabled: bool
    late_night_care_enabled: bool
    timezone: str
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_edition(self.preferred_edition)
        validate_cupid_layout(self.cupid_layout)
        validate_wallpaper_preset(self.wallpaper_preset)
        validate_wallpaper_blur(self.wallpaper_blur)
        validate_wallpaper_vignette(self.wallpaper_vignette)
        validate_timezone(self.timezone)
        if isinstance(self.version, bool) or self.version < 1:
            raise PreferencesValidationError("version must be a positive integer")

    @property
    def has_custom_wallpaper(self) -> bool:
        return bool(self.wallpaper_object_key)


@dataclass(frozen=True, slots=True)
class PreferencesPatch:
    """Partial update command for UserPreferences."""

    preferred_edition: Edition | None = None
    cupid_layout: CupidLayout | None = None
    wallpaper_preset: str | None = None
    wallpaper_blur: int | None = None
    wallpaper_vignette: int | None = None
    app_lock_enabled: bool | None = None
    late_night_care_enabled: bool | None = None
    timezone: str | None = None


def default_user_preferences(
    *,
    user_id: UUID,
    timezone: str,
    now: datetime,
) -> UserPreferences:
    return UserPreferences(
        user_id=user_id,
        preferred_edition=DEFAULT_EDITION,
        cupid_layout=DEFAULT_CUPID_LAYOUT,
        wallpaper_preset=DEFAULT_WALLPAPER_PRESET,
        wallpaper_object_key=None,
        wallpaper_blur=DEFAULT_WALLPAPER_BLUR,
        wallpaper_vignette=DEFAULT_WALLPAPER_VIGNETTE,
        app_lock_enabled=False,
        late_night_care_enabled=True,
        timezone=validate_timezone(timezone),
        version=1,
        updated_at=now,
    )


def apply_preferences_patch(
    current: UserPreferences,
    patch: PreferencesPatch,
    *,
    expected_version: int | None,
    now: datetime,
) -> UserPreferences:
    """Validate optimistic lock & domain invariants, returning next version of UserPreferences."""
    if expected_version is not None and expected_version != current.version:
        raise PreferencesVersionConflictError(
            expected_version=expected_version,
            current_version=current.version,
        )

    next_edition = (
        validate_edition(patch.preferred_edition)
        if patch.preferred_edition is not None
        else current.preferred_edition
    )
    next_layout = (
        validate_cupid_layout(patch.cupid_layout)
        if patch.cupid_layout is not None
        else current.cupid_layout
    )
    next_preset = (
        validate_wallpaper_preset(patch.wallpaper_preset)
        if patch.wallpaper_preset is not None
        else current.wallpaper_preset
    )
    next_blur = (
        validate_wallpaper_blur(patch.wallpaper_blur)
        if patch.wallpaper_blur is not None
        else current.wallpaper_blur
    )
    next_vignette = (
        validate_wallpaper_vignette(patch.wallpaper_vignette)
        if patch.wallpaper_vignette is not None
        else current.wallpaper_vignette
    )
    next_app_lock = (
        patch.app_lock_enabled if patch.app_lock_enabled is not None else current.app_lock_enabled
    )
    next_late_night = (
        patch.late_night_care_enabled
        if patch.late_night_care_enabled is not None
        else current.late_night_care_enabled
    )
    next_tz = validate_timezone(patch.timezone) if patch.timezone is not None else current.timezone

    return replace(
        current,
        preferred_edition=next_edition,
        cupid_layout=next_layout,
        wallpaper_preset=next_preset,
        wallpaper_blur=next_blur,
        wallpaper_vignette=next_vignette,
        app_lock_enabled=next_app_lock,
        late_night_care_enabled=next_late_night,
        timezone=next_tz,
        version=current.version + 1,
        updated_at=now,
    )
