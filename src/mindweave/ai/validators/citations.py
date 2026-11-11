"""Deterministic citation verification and streaming tag processor (Invariant I4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from mindweave.ai.schemas.contracts import CompanionChatOutput

_CITATION_TAG_RE = re.compile(r"(?:\[rec_|【rec_)([^\]】]+)(?:\]|】)")


@dataclass(frozen=True, slots=True)
class CitationValidationResult:
    """Verified text and deduplicated in-scope cited record UUIDs."""

    cleaned_text: str
    valid_record_ids: list[UUID]
    stripped_tags: list[str]


def _try_parse_uuid(raw: str) -> UUID | None:
    candidate = raw.strip()
    if candidate.startswith("rec_"):
        candidate = candidate[4:]
    try:
        return UUID(candidate)
    except ValueError:
        return None


def validate_citations(
    text: str,
    declared_record_ids: list[UUID],
    allowed_record_ids: set[UUID],
    *,
    strip_valid_tags: bool = False,
) -> CitationValidationResult:
    """Verify `cited ⊆ allowed_record_ids` and strip any hallucinated `[rec_...]` tags."""
    ordered_valid: list[UUID] = []
    seen: set[UUID] = set()
    stripped: list[str] = []

    def _add_valid(uid: UUID) -> None:
        if uid in allowed_record_ids and uid not in seen:
            seen.add(uid)
            ordered_valid.append(uid)

    def _replace_tag(match: re.Match[str]) -> str:
        raw_inner = match.group(1)
        uid = _try_parse_uuid(raw_inner)
        if uid is not None and uid in allowed_record_ids:
            _add_valid(uid)
            return "" if strip_valid_tags else f"[rec_{uid}]"
        stripped.append(match.group(0))
        return ""

    cleaned = _CITATION_TAG_RE.sub(_replace_tag, text)
    # Collapse redundant spaces left before Chinese/ASCII punctuation after stripping tags
    cleaned = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    for declared in declared_record_ids:
        if declared in allowed_record_ids:
            _add_valid(declared)
        else:
            stripped.append(f"[rec_{declared}]")

    return CitationValidationResult(
        cleaned_text=cleaned,
        valid_record_ids=ordered_valid,
        stripped_tags=stripped,
    )


def enforce_companion_citations(
    output: CompanionChatOutput,
    allowed_record_ids: set[UUID],
) -> CompanionChatOutput:
    """Return a copy of `CompanionChatOutput` with verified citations only (Invariant I4)."""
    res = validate_citations(
        output.reply_text,
        output.cited_record_ids,
        allowed_record_ids,
        strip_valid_tags=False,
    )
    return output.model_copy(
        update={
            "reply_text": res.cleaned_text,
            "cited_record_ids": res.valid_record_ids,
        }
    )


@dataclass
class StreamingCitationProcessor:
    """Incremental chunk processor that strips out-of-scope `[rec_...]` tags across chunk boundaries."""

    allowed_record_ids: set[UUID]
    strip_valid_tags: bool = False
    verified_record_ids: list[UUID] = field(default_factory=list)
    stripped_tags: list[str] = field(default_factory=list)
    _buffer: str = ""
    _seen: set[UUID] = field(default_factory=set)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        data = self._buffer + chunk
        self._buffer = ""
        out_parts: list[str] = []
        idx = 0
        length = len(data)

        while idx < length:
            ch = data[idx]
            if ch in ("[", "【"):
                close_char = "]" if ch == "[" else "】"
                end_idx = data.find(close_char, idx + 1)
                if end_idx == -1:
                    # Wait for closing bracket unless buffer is abnormally long (not a tag)
                    if length - idx <= 64:
                        self._buffer = data[idx:]
                        break
                    out_parts.append(ch)
                    idx += 1
                    continue

                tag = data[idx : end_idx + 1]
                inner = data[idx + 1 : end_idx]
                if inner.startswith("rec_"):
                    uid = _try_parse_uuid(inner[4:])
                    if uid is not None and uid in self.allowed_record_ids:
                        if uid not in self._seen:
                            self._seen.add(uid)
                            self.verified_record_ids.append(uid)
                        if not self.strip_valid_tags:
                            out_parts.append(f"[rec_{uid}]")
                    else:
                        self.stripped_tags.append(tag)
                else:
                    out_parts.append(tag)
                idx = end_idx + 1
            else:
                out_parts.append(ch)
                idx += 1

        return "".join(out_parts)

    def flush(self) -> str:
        if not self._buffer:
            return ""
        rem = self._buffer
        self._buffer = ""
        res = validate_citations(
            rem,
            [],
            self.allowed_record_ids,
            strip_valid_tags=self.strip_valid_tags,
        )
        for uid in res.valid_record_ids:
            if uid not in self._seen:
                self._seen.add(uid)
                self.verified_record_ids.append(uid)
        self.stripped_tags.extend(res.stripped_tags)
        return res.cleaned_text
