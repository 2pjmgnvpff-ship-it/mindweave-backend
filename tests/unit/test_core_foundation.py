"""Unit tests for M0 core foundation (UUIDv7, EnvelopeCipher, LexoRank, PII masker, Redaction, Guardrails)."""

from __future__ import annotations

import pytest

from mindweave.core.config import Settings
from mindweave.core.crypto import (
    CryptoError,
    EnvelopeCipher,
    EnvKekProvider,
    KeyShreddedError,
    normalize_alias_for_index,
)
from mindweave.core.dto import contains_forbidden_dev_jargon
from mindweave.core.ids import is_uuid7, new_id
from mindweave.core.logging import sanitize_log_value
from mindweave.shared.enums import Tier
from mindweave.shared.lexorank import initial_ranks, rank_after, rank_before, rank_between
from mindweave.shared.pii import is_valid_cn_id18, mask_hard_pii


def test_uuid7_monotonicity() -> None:
    ids = [new_id() for _ in range(500)]
    assert all(is_uuid7(u) for u in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == 500


def test_envelope_cipher_aad_binding_and_crypto_shredding() -> None:
    kek = EnvKekProvider(kek_hex="aa" * 32, kek_id="kek-test")
    cipher = EnvelopeCipher(kek)
    u1, u2 = new_id(), new_id()
    row_a, row_b = new_id(), new_id()

    wrapped1, kid = cipher.generate_wrapped_dek(u1)
    dek1 = cipher.load_dek(user_id=u1, wrapped_dek=wrapped1, kek_id=kid)

    ct = cipher.encrypt_str(
        dek1, "生理期前三天忌冰美式", user_id=u1, table="facts", column="content_enc", row_id=row_a
    )
    assert b"\xe5\x86\xb0" not in ct
    pt = cipher.decrypt_str(dek1, ct, user_id=u1, table="facts", column="content_enc", row_id=row_a)
    assert pt == "生理期前三天忌冰美式"

    # AAD prevents transplanting ciphertext to another row, column, table, or user!
    with pytest.raises(CryptoError):
        cipher.decrypt_str(dek1, ct, user_id=u1, table="facts", column="content_enc", row_id=row_b)
    with pytest.raises(CryptoError):
        cipher.decrypt_str(dek1, ct, user_id=u1, table="facts", column="title_enc", row_id=row_a)
    with pytest.raises(CryptoError):
        cipher.decrypt_str(dek1, ct, user_id=u2, table="facts", column="content_enc", row_id=row_a)

    # Blind index is deterministic per user, distinct across users
    wrapped2, _ = cipher.generate_wrapped_dek(u2)
    dek2 = cipher.load_dek(user_id=u2, wrapped_dek=wrapped2, kek_id=kid)
    norm = normalize_alias_for_index(" 沈 清秋 ")
    h1 = cipher.compute_blind_index(dek1, user_id=u1, domain="alias", normalized_value=norm)
    h1_again = cipher.compute_blind_index(
        dek1, user_id=u1, domain="alias", normalized_value="沈清秋"
    )
    h2 = cipher.compute_blind_index(dek2, user_id=u2, domain="alias", normalized_value="沈清秋")
    assert h1 == h1_again
    assert h1 != h2

    # Crypto-shredding immediately rejects loading even if wrapped_dek were cached
    with pytest.raises(KeyShreddedError):
        cipher.load_dek(user_id=u1, wrapped_dek=None, kek_id=kid, destroyed_at="2026-11-12")


def test_lexorank_strict_ordering_across_insertions() -> None:
    ranks = initial_ranks(5)
    assert ranks == sorted(ranks)
    # Prepend 20 times (like demoting gold -> crystal to front of casualIds)
    cur_first = ranks[0]
    for _ in range(20):
        prev = rank_before(cur_first)
        assert prev < cur_first
        cur_first = prev

    # Append 20 times
    cur_last = ranks[-1]
    for _ in range(20):
        nxt = rank_after(cur_last)
        assert cur_last < nxt
        cur_last = nxt

    # Repeated midpoint bisection 30 times between adjacent items
    left, right = ranks[1], ranks[2]
    for _ in range(30):
        mid = rank_between(left, right)
        assert left < mid < right
        right = mid


def test_hard_pii_masker_preserves_names_and_dates() -> None:
    # Construct valid 18-digit mainland ID checksum for 11010519950618123X
    prefix17 = "11010519950618123"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check = "10X98765432"[sum(int(d) * w for d, w in zip(prefix17, weights, strict=True)) % 11]
    valid_id = prefix17 + check
    assert is_valid_cn_id18(valid_id)

    sample = (
        f"沈清秋说去镰仓机票用她的身份证 {valid_id}，"
        f"卡号 6222 0212 3456 7890 129（测试），家里门锁密码是 884812，12月24日出发。"
    )
    # Compute Luhn check digit for 622202123456789012x
    base18 = "622202123456789012"
    for d in "0123456789":
        from mindweave.shared.pii import is_luhn_valid

        if is_luhn_valid(base18 + d):
            valid_card = f"6222 0212 3456 7890 12{d}"
            break
    sample = sample.replace("6222 0212 3456 7890 129", valid_card)

    res = mask_hard_pii(sample)
    assert res.masked_id_cards == 1
    assert res.masked_bank_cards == 1
    assert res.masked_secrets == 1
    assert "沈清秋" in res.masked_text
    assert "镰仓" in res.masked_text
    assert "12月24日" in res.masked_text
    assert valid_id not in res.masked_text
    assert "884812" not in res.masked_text


def test_zero_pii_log_redaction_and_prod_cn_guardrails() -> None:
    sanitized = sanitize_log_value(
        {
            "userId": "u-1",
            "rawText": "林知夏又甩锅",
            "displayName": "沈清秋",
            "nested": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.secret"},
        }
    )
    assert sanitized["rawText"] == "[REDACTED]"
    assert sanitized["displayName"] == "[REDACTED]"
    assert sanitized["nested"]["Authorization"] == "[REDACTED]"

    assert contains_forbidden_dev_jargon("系统自动归档：海边约定") == "系统自动归档"
    assert contains_forbidden_dev_jargon("周五午后 · 海边约定") is None
    assert Tier.from_any("love") is Tier.GOLD
    assert Tier.from_any("hate") is Tier.SILVER
    assert Tier.from_any("matter") is Tier.CRYSTAL

    with pytest.raises(ValueError, match="Compliance Gate M6"):
        Settings(
            environment="prod_cn",
            ai_primary_provider="gemini",
            kek_hex="bb" * 32,
            jwt_secret="x" * 40,
            allow_dev_mock_apple_auth=False,
        )
