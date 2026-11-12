"""Unit tests for Identity, Apple JWKS Verifier, Auth Domain Policies, Preferences, and EXIF Sanitizer."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from mindweave.core.errors import PayloadTooLargeError, UnauthorizedError, ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.modules.identity.api.schemas import (
    AppleAuthRequest,
    EmailOtpRequest,
    RecordConsentRequest,
)
from mindweave.modules.identity.domain import (
    InvalidEmailError,
    InvalidOtpCodeError,
    InvalidPolicyVersionError,
    OtpChallengeState,
    OtpPurpose,
    RefreshTokenState,
    evaluate_otp_challenge_state,
    evaluate_refresh_token_state,
    normalize_email,
    normalize_otp_code,
    normalize_policy_version,
)
from mindweave.modules.identity.infra.apple_verifier import (
    APPLE_ISSUER,
    APPLE_JWKS_URL,
    AppleJwksTokenVerifier,
)
from mindweave.modules.preferences.api.schemas import (
    PreferencesResponse,
    UpdatePreferencesRequest,
)
from mindweave.modules.preferences.domain import (
    PreferencesPatch,
    PreferencesValidationError,
    PreferencesVersionConflictError,
    apply_preferences_patch,
    default_user_preferences,
    validate_cupid_layout,
    validate_edition,
    validate_timezone,
    validate_wallpaper_blur,
    validate_wallpaper_vignette,
)
from mindweave.modules.preferences.infra.image_sanitizer import sanitize_wallpaper_image
from mindweave.shared.enums import ConsentKind, CupidLayout, Edition


def _generate_rsa_jwk(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_dict = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return private_key, jwk_dict


def test_identity_email_otp_and_policy_normalization_zero_pii_errors() -> None:
    assert normalize_email("  Shen.QingQiu@Example.COM ") == "shen.qingqiu@example.com"

    bad_samples = [
        "",
        "   ",
        "plainaddress",
        "@example.com",
        "alice@",
        "alice@localhost",
        "alice..bob@example.com",
        f"{'a' * 65}@example.com",
    ]
    for bad in bad_samples:
        with pytest.raises(InvalidEmailError) as exc_info:
            normalize_email(bad)
        # Invariant I10: raw user input must never be echoed in domain error text
        if bad.strip():
            assert bad.strip() not in str(exc_info.value)

    assert normalize_otp_code(" 048291 ") == "048291"
    with pytest.raises(InvalidOtpCodeError):
        normalize_otp_code("12345")
    with pytest.raises(InvalidOtpCodeError):
        normalize_otp_code("12ab56")

    assert normalize_policy_version(" 2026.11-pipl ") == "2026.11-pipl"
    with pytest.raises(InvalidPolicyVersionError):
        normalize_policy_version("   ")
    with pytest.raises(InvalidPolicyVersionError):
        normalize_policy_version("v" * 33)


def test_otp_and_refresh_state_policies() -> None:
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    future = now + timedelta(minutes=10)
    past = now - timedelta(seconds=1)

    assert (
        evaluate_otp_challenge_state(
            consumed_at=None,
            expires_at=future,
            attempts=4,
            max_attempts=5,
            now=now,
        )
        is OtpChallengeState.READY
    )
    assert (
        evaluate_otp_challenge_state(
            consumed_at=now,
            expires_at=future,
            attempts=1,
            max_attempts=5,
            now=now,
        )
        is OtpChallengeState.CONSUMED
    )
    assert (
        evaluate_otp_challenge_state(
            consumed_at=None,
            expires_at=now,
            attempts=0,
            max_attempts=5,
            now=now,
        )
        is OtpChallengeState.EXPIRED
    )
    assert (
        evaluate_otp_challenge_state(
            consumed_at=None,
            expires_at=future,
            attempts=5,
            max_attempts=5,
            now=now,
        )
        is OtpChallengeState.LOCKED_OUT
    )

    assert (
        evaluate_refresh_token_state(
            expires_at=future,
            revoked_at=None,
            replaced_by_id=None,
            now=now,
        )
        is RefreshTokenState.VALID
    )
    assert (
        evaluate_refresh_token_state(
            expires_at=past,
            revoked_at=None,
            replaced_by_id=None,
            now=now,
        )
        is RefreshTokenState.EXPIRED
    )
    assert (
        evaluate_refresh_token_state(
            expires_at=future,
            revoked_at=now,
            replaced_by_id=None,
            now=now,
        )
        is RefreshTokenState.REPLAY_DETECTED
    )
    assert (
        evaluate_refresh_token_state(
            expires_at=future,
            revoked_at=None,
            replaced_by_id=new_id(),
            now=now,
        )
        is RefreshTokenState.REPLAY_DETECTED
    )


@pytest.mark.asyncio
async def test_apple_jwks_verifier_rs256_caching_key_rotation_and_dev_guardrails() -> None:
    priv_1, jwk_1 = _generate_rsa_jwk("apple-kid-1")
    priv_2, jwk_2 = _generate_rsa_jwk("apple-kid-2")
    attacker_priv, _ = _generate_rsa_jwk("apple-kid-1")

    active_jwks: list[dict[str, Any]] = [jwk_1]
    fetch_count = 0

    def _mock_jwks_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        assert str(request.url) == APPLE_JWKS_URL
        fetch_count += 1
        return httpx.Response(200, json={"keys": active_jwks})

    now = datetime(2026, 11, 12, 8, 0, 0, tzinfo=UTC)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_jwks_handler)) as client:
        verifier = AppleJwksTokenVerifier(
            allow_dev_mock=False,
            allowed_audiences=("app.mindweave.ios",),
            http_client=client,
        )

        # Dev mock token MUST be rejected when allow_dev_mock=False
        with pytest.raises(UnauthorizedError, match="disabled"):
            await verifier.verify_identity_token("dev_apple:test-sub", now=now)

        valid_jwt = jwt.encode(
            {
                "iss": APPLE_ISSUER,
                "sub": "001928.apple.user",
                "aud": "app.mindweave.ios",
                "email": "AppleUser@Privaterelay.Appleid.com",
                "email_verified": "true",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
            priv_1,
            algorithm="RS256",
            headers={"kid": "apple-kid-1"},
        )

        verified_a = await verifier.verify_identity_token(valid_jwt, now=now)
        verified_b = await verifier.verify_identity_token(valid_jwt, now=now)
        assert verified_a.subject == "001928.apple.user"
        assert verified_a.email == "appleuser@privaterelay.appleid.com"
        assert verified_a.email_verified is True
        assert verified_b.subject == "001928.apple.user"
        # JWKS was cached across repeated verifications
        assert fetch_count == 1

        # Expired token is rejected
        with pytest.raises(UnauthorizedError, match="expired"):
            await verifier.verify_identity_token(valid_jwt, now=now + timedelta(minutes=11))

        # Forged signature with same kid is rejected
        forged_jwt = jwt.encode(
            {
                "iss": APPLE_ISSUER,
                "sub": "001928.apple.user",
                "aud": "app.mindweave.ios",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
            attacker_priv,
            algorithm="RS256",
            headers={"kid": "apple-kid-1"},
        )
        with pytest.raises(UnauthorizedError):
            await verifier.verify_identity_token(forged_jwt, now=now)

        # Wrong issuer is rejected
        wrong_iss_jwt = jwt.encode(
            {
                "iss": "https://evil.example.com",
                "sub": "001928.apple.user",
                "aud": "app.mindweave.ios",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
            priv_1,
            algorithm="RS256",
            headers={"kid": "apple-kid-1"},
        )
        with pytest.raises(UnauthorizedError):
            await verifier.verify_identity_token(wrong_iss_jwt, now=now)

        # Key rotation: Apple adds kid-2; verifier refreshes JWKS and verifies!
        active_jwks.append(jwk_2)
        rotated_jwt = jwt.encode(
            {
                "iss": APPLE_ISSUER,
                "sub": "009999.rotated.user",
                "aud": "app.mindweave.ios",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
            priv_2,
            algorithm="RS256",
            headers={"kid": "apple-kid-2"},
        )
        verified_rot = await verifier.verify_identity_token(rotated_jwt, now=now)
        assert verified_rot.subject == "009999.rotated.user"
        assert fetch_count == 2

    # When allow_dev_mock=True, deterministic dev_apple:<sub_id> succeeds
    dev_verifier = AppleJwksTokenVerifier(allow_dev_mock=True)
    dev_ident = await dev_verifier.verify_identity_token("dev_apple:local-dev-user-42", now=now)
    assert dev_ident.subject == "local-dev-user-42"
    assert dev_ident.email is None


def test_preferences_domain_invariants_and_optimistic_locking() -> None:
    for ed in ("cotton", "dotgrid", "parchment", "traveler", "hobonichi", "museglass"):
        assert validate_edition(ed) == Edition(ed)
    with pytest.raises(PreferencesValidationError):
        validate_edition("neon_cyberpunk")

    for layout in ("a", "b", "c", "d"):
        assert validate_cupid_layout(layout) == CupidLayout(layout)
    with pytest.raises(PreferencesValidationError):
        validate_cupid_layout("e")

    assert validate_wallpaper_blur(0) == 0
    assert validate_wallpaper_blur(48) == 48
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_blur(-1)
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_blur(49)
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_blur(True)

    assert validate_wallpaper_vignette(0) == 0
    assert validate_wallpaper_vignette(85) == 85
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_vignette(-1)
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_vignette(86)
    with pytest.raises(PreferencesValidationError):
        validate_wallpaper_vignette(False)

    assert validate_timezone("Asia/Shanghai") == "Asia/Shanghai"
    assert validate_timezone("America/New_York") == "America/New_York"
    assert validate_timezone("UTC") == "UTC"
    with pytest.raises(PreferencesValidationError):
        validate_timezone("Mars/Olympus_Mons")
    with pytest.raises(PreferencesValidationError):
        validate_timezone("")

    u_id = new_id()
    t0 = datetime(2026, 11, 12, 4, 0, 0, tzinfo=UTC)
    prefs = default_user_preferences(user_id=u_id, timezone="Asia/Shanghai", now=t0)
    assert prefs.version == 1
    assert prefs.has_custom_wallpaper is False

    t1 = t0 + timedelta(minutes=5)
    updated = apply_preferences_patch(
        prefs,
        PreferencesPatch(
            preferred_edition=Edition.MUSEGLASS,
            cupid_layout=CupidLayout.D,
            wallpaper_blur=36,
            wallpaper_vignette=65,
            timezone="Asia/Tokyo",
        ),
        expected_version=1,
        now=t1,
    )
    assert updated.version == 2
    assert updated.preferred_edition is Edition.MUSEGLASS
    assert updated.cupid_layout is CupidLayout.D
    assert updated.wallpaper_blur == 36
    assert updated.wallpaper_vignette == 65
    assert updated.timezone == "Asia/Tokyo"

    with pytest.raises(PreferencesVersionConflictError) as conflict_exc:
        apply_preferences_patch(
            updated,
            PreferencesPatch(wallpaper_blur=10),
            expected_version=1,
            now=t1,
        )
    assert conflict_exc.value.expected_version == 1
    assert conflict_exc.value.current_version == 2


def test_wallpaper_image_sanitizer_strips_exif_and_enforces_magic_bytes() -> None:
    # 1. JPEG with EXIF camera/comment/orientation tags
    raw_img = Image.new("RGB", (64, 32), color=(210, 180, 150))
    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneVendor"  # Make
    exif[0x0110] = "ModelXYZ-GPS-Leak"  # Model
    exif[0x0112] = 6  # Orientation: Rotate 90 CW
    jpeg_buf = io.BytesIO()
    raw_img.save(jpeg_buf, format="JPEG", exif=exif.tobytes())
    jpeg_with_exif = jpeg_buf.getvalue()
    assert b"SecretPhoneVendor" in jpeg_with_exif

    sanitized_jpeg = sanitize_wallpaper_image(jpeg_with_exif, max_bytes=1024 * 1024)
    assert sanitized_jpeg.content_type == "image/jpeg"
    assert sanitized_jpeg.format_name == "JPEG"
    # Orientation 6 transposed 64x32 -> 32x64 cleanly before stripping EXIF
    assert (sanitized_jpeg.width, sanitized_jpeg.height) == (32, 64)
    assert b"SecretPhoneVendor" not in sanitized_jpeg.data
    assert b"ModelXYZ-GPS-Leak" not in sanitized_jpeg.data
    with Image.open(io.BytesIO(sanitized_jpeg.data)) as reloaded:
        assert len(reloaded.getexif()) == 0

    # 2. PNG with embedded text chunks + alpha channel
    png_img = Image.new("RGBA", (40, 40), color=(100, 120, 140, 180))
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Author", "PrivateUserName")
    png_info.add_text("GPS", "31.2304N,121.4737E")
    png_buf = io.BytesIO()
    png_img.save(png_buf, format="PNG", pnginfo=png_info)
    png_with_meta = png_buf.getvalue()
    assert b"PrivateUserName" in png_with_meta

    sanitized_png = sanitize_wallpaper_image(png_with_meta, max_bytes=1024 * 1024)
    assert sanitized_png.content_type == "image/png"
    assert b"PrivateUserName" not in sanitized_png.data
    assert b"31.2304N" not in sanitized_png.data
    with Image.open(io.BytesIO(sanitized_png.data)) as reloaded_png:
        assert reloaded_png.mode == "RGBA"
        assert "Author" not in reloaded_png.info

    # 3. WEBP with EXIF
    webp_buf = io.BytesIO()
    raw_img.save(webp_buf, format="WEBP", exif=exif.tobytes())
    sanitized_webp = sanitize_wallpaper_image(webp_buf.getvalue(), max_bytes=1024 * 1024)
    assert sanitized_webp.content_type == "image/webp"
    assert b"SecretPhoneVendor" not in sanitized_webp.data

    # 4. Reject GIF, arbitrary bytes, empty bytes, and oversized files
    gif_buf = io.BytesIO()
    raw_img.save(gif_buf, format="GIF")
    with pytest.raises(ValidationFailedError, match="Unsupported image format"):
        sanitize_wallpaper_image(gif_buf.getvalue(), max_bytes=1024 * 1024)

    with pytest.raises(ValidationFailedError):
        sanitize_wallpaper_image(b"not-an-image-at-all", max_bytes=1024 * 1024)

    with pytest.raises(ValidationFailedError):
        sanitize_wallpaper_image(b"", max_bytes=1024 * 1024)

    with pytest.raises(PayloadTooLargeError):
        sanitize_wallpaper_image(sanitized_jpeg.data, max_bytes=10)


def test_camelcase_dtos_and_pydantic_validation() -> None:
    apple_req = AppleAuthRequest.model_validate(
        {"identityToken": "dev_apple:123", "displayEmail": "user@example.com"}
    )
    assert apple_req.identity_token == "dev_apple:123"

    otp_req = EmailOtpRequest.model_validate({"email": "a@b.co", "purpose": "reauth"})
    assert otp_req.purpose is OtpPurpose.REAUTH

    consent_req = RecordConsentRequest.model_validate(
        {"consentKind": "ai_processing", "policyVersion": "v1.0", "granted": True}
    )
    assert consent_req.consent_kind is ConsentKind.AI_PROCESSING

    with pytest.raises(ValidationError):
        UpdatePreferencesRequest.model_validate({"wallpaperBlur": 49})
    with pytest.raises(ValidationError):
        UpdatePreferencesRequest.model_validate({"wallpaperVignette": 86})
    with pytest.raises(ValidationError):
        UpdatePreferencesRequest.model_validate({"timezone": "Invalid/Timezone"})

    prefs = default_user_preferences(
        user_id=new_id(),
        timezone="Asia/Shanghai",
        now=datetime(2026, 11, 12, tzinfo=UTC),
    )
    dumped = PreferencesResponse.from_domain(
        prefs,
        wallpaper_url="/v1/me/wallpaper?uid=ignored&exp=1&sig=abc",
    ).model_dump(by_alias=True)
    assert dumped["wallpaperUrl"] is None
    assert set(dumped.keys()) == {
        "preferredEdition",
        "cupidLayout",
        "wallpaperPreset",
        "hasCustomWallpaper",
        "wallpaperUrl",
        "wallpaperBlur",
        "wallpaperVignette",
        "appLockEnabled",
        "lateNightCareEnabled",
        "timezone",
        "version",
    }

    prefs_with_wp = default_user_preferences(
        user_id=prefs.user_id,
        timezone="Asia/Shanghai",
        now=datetime(2026, 11, 12, tzinfo=UTC),
    )
    from dataclasses import replace

    prefs_custom = replace(prefs_with_wp, wallpaper_object_key=f"{prefs.user_id}/wallpaper/obj1")
    dumped_custom = PreferencesResponse.from_domain(
        prefs_custom,
        wallpaper_url="/v1/me/wallpaper?uid=test&exp=999&sig=deadbeef",
    ).model_dump(by_alias=True)
    assert dumped_custom["hasCustomWallpaper"] is True
    assert dumped_custom["wallpaperUrl"] == "/v1/me/wallpaper?uid=test&exp=999&sig=deadbeef"
