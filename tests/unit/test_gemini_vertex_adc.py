"""Unit tests for Google Cloud Vertex AI ADC authentication on GeminiAdapter & Settings."""

from __future__ import annotations

from typing import Any

import pytest
from google.auth import exceptions as google_auth_exceptions

from mindweave.ai.adapters import gemini as gemini_mod
from mindweave.ai.adapters.base import AdapterPermanentError
from mindweave.ai.adapters.gemini import GeminiAdapter
from mindweave.core.config import Settings
from mindweave.main import _is_cloud_asr_ready, _is_live_model_configured


def _staging_vertex_base(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "environment": "staging",
        "debug": False,
        "allow_dev_mock_apple_auth": False,
        "expose_dev_otp_in_response": False,
        "push_apns_adapter": "disabled",
        "push_web_push_adapter": "disabled",
        "email_provider": "resend",
        "resend_api_key": "re_test_valid_key_1234567890",
        "email_from_address": "noreply@mail.mindweave.app",
        "ai_primary_provider": "gemini",
        "gemini_api_key": "",
        "gemini_use_vertexai": True,
        "gemini_vertex_project": "project-188d37ff-1d39-4847-801",
    }
    base.update(overrides)
    return base


def test_settings_accepts_vertex_adc_without_api_key() -> None:
    s = Settings(**_staging_vertex_base())
    assert s.is_gemini_configured() is True
    assert s.gemini_auth_mode == "vertex_adc"
    assert s.gemini_vertex_location == "global"
    assert _is_live_model_configured(s) is True
    assert _is_cloud_asr_ready(s) is True


def test_settings_vertex_adc_resolves_google_cloud_project_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-env-proj-999")
    s = Settings(**_staging_vertex_base(gemini_vertex_project=""))
    assert s.gemini_vertex_project == "gcp-env-proj-999"
    assert s.gemini_auth_mode == "vertex_adc"


def test_settings_vertex_adc_fails_closed_without_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    with pytest.raises(ValueError, match="Missing GCP project for Gemini Vertex AI ADC"):
        Settings(**_staging_vertex_base(gemini_vertex_project=""))


def test_settings_prod_cn_still_forbids_gemini_vertex_adc() -> None:
    with pytest.raises(ValueError, match="Compliance Gate M6"):
        Settings(**_staging_vertex_base(environment="prod_cn"))


def test_gemini_adapter_constructs_vertexai_adc_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(gemini_mod.genai, "Client", _FakeClient)
    adapter = GeminiAdapter(
        api_key="",
        use_vertexai=True,
        vertex_project="project-188d37ff-1d39-4847-801",
        vertex_location="global",
    )
    client = adapter._get_client()
    assert isinstance(client, _FakeClient)
    assert captured == {
        "vertexai": True,
        "project": "project-188d37ff-1d39-4847-801",
        "location": "global",
    }
    assert "api_key" not in captured


def test_gemini_adapter_maps_adc_auth_error_to_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_kwargs: Any) -> Any:
        raise google_auth_exceptions.DefaultCredentialsError("ADC missing")

    monkeypatch.setattr(gemini_mod.genai, "Client", _boom)
    adapter = GeminiAdapter(
        use_vertexai=True,
        vertex_project="project-188d37ff-1d39-4847-801",
        vertex_location="global",
    )
    with pytest.raises(AdapterPermanentError, match="ADC authentication failure"):
        adapter._get_client()

    mapped = adapter._map_exception(google_auth_exceptions.RefreshError("expired"))
    assert isinstance(mapped, AdapterPermanentError)
