"""Preferences API layer exports."""

from mindweave.modules.preferences.api.router import router
from mindweave.modules.preferences.api.schemas import (
    PreferencesResponse,
    UpdatePreferencesRequest,
)

__all__ = ["PreferencesResponse", "UpdatePreferencesRequest", "router"]
