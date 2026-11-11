"""Deterministic pre-LLM hard-PII masker (PIA v0.2 Shield 3).

Masks high-risk financial/government credentials (18-digit mainland ID card with valid
ISO 7064 checksum, Luhn-valid 16-19 digit bank card numbers, explicit password/secret
literals) BEFORE sending text to external LLM APIs, while preserving nicknames, human
names, dates, and city names so coreference resolution remains accurate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID18_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID18_CHECKSUM = "10X98765432"

# Candidate 18-char mainland resident ID: 6-digit region + 19xx/20xx YYYYMMDD + 3 seq + checksum
_ID18_RE = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)

# Candidate 16-19 digit bank card (allow single spaces or hyphens between 4-digit groups)
_BANK_CARD_RE = re.compile(r"(?<!\d)([3-6]\d{3}(?:[\s-]?\d{4}){3}(?:[\s-]?\d{1,3})?)(?!\d)")

# Explicit password / PIN / CVV disclosure in prose
_PASSWORD_RE = re.compile(
    r"((?:密码|开机密码|锁屏密码|支付密码|银行卡密码|取款密码|PIN码|口令)\s*(?:是|为|改为|[：:=])\s*)([^\s，,。；;！!？?\n]{4,32})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PiiMaskResult:
    masked_text: str
    masked_id_cards: int = 0
    masked_bank_cards: int = 0
    masked_secrets: int = 0

    @property
    def any_masked(self) -> bool:
        return (self.masked_id_cards + self.masked_bank_cards + self.masked_secrets) > 0


def is_valid_cn_id18(candidate: str) -> bool:
    if len(candidate) != 18:
        return False
    digits = candidate[:17]
    if not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits, _ID18_WEIGHTS, strict=True))
    expected = _ID18_CHECKSUM[total % 11]
    return candidate[17].upper() == expected


def is_luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not (16 <= len(digits) <= 19):
        return False
    # Reject trivial repeated digits
    if len(set(digits)) == 1:
        return False
    total = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask_hard_pii(text: str) -> PiiMaskResult:
    if not text:
        return PiiMaskResult(masked_text=text)

    id_count = 0
    bank_count = 0
    secret_count = 0

    def _replace_id(match: re.Match[str]) -> str:
        nonlocal id_count
        raw = match.group(1)
        if is_valid_cn_id18(raw):
            id_count += 1
            return "[REDACTED_ID_CARD]"
        return raw

    out = _ID18_RE.sub(_replace_id, text)

    def _replace_bank(match: re.Match[str]) -> str:
        nonlocal bank_count
        raw = match.group(1)
        compact = raw.replace(" ", "").replace("-", "")
        if is_luhn_valid(compact):
            bank_count += 1
            return "[REDACTED_BANK_CARD]"
        return raw

    out = _BANK_CARD_RE.sub(_replace_bank, out)

    def _replace_secret(match: re.Match[str]) -> str:
        nonlocal secret_count
        secret_count += 1
        return f"{match.group(1)}[REDACTED_SECRET]"

    out = _PASSWORD_RE.sub(_replace_secret, out)

    return PiiMaskResult(
        masked_text=out,
        masked_id_cards=id_count,
        masked_bank_cards=bank_count,
        masked_secrets=secret_count,
    )
