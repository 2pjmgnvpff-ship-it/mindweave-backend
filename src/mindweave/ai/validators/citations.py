"""Deterministic citation verification and streaming tag processor (Invariant I4)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from mindweave.ai.schemas.contracts import CompanionChatOutput

_CITATION_TAG_RE = re.compile(r"(?:\[\s*rec_|【\s*rec_)([^\[\]【】\r\n]+)(?:\]|】)", re.IGNORECASE)
_CODE_SPAN_OR_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\r\n]+`")
_CLOSING_OR_PUNCT_CHARS = frozenset("，。！？；：,.!?;:]】")


@dataclass(frozen=True, slots=True)
class CitationValidationResult:
    """Verified text and deduplicated in-scope cited record UUIDs."""

    cleaned_text: str
    valid_record_ids: list[UUID]
    stripped_tags: list[str]


def _try_parse_uuid(raw: str) -> UUID | None:
    candidate = raw.strip()
    if candidate.lower().startswith("rec_"):
        candidate = candidate[4:].strip()
    try:
        return UUID(candidate)
    except ValueError:
        return None


def validate_citations(
    text: str,
    declared_record_ids: Sequence[UUID] = (),
    allowed_record_ids: set[UUID] | frozenset[UUID] = frozenset(),
    *,
    strip_valid_tags: bool = False,
) -> CitationValidationResult:
    """Verify `cited ⊆ allowed_record_ids` and strip `[rec_...]` tags outside code spans."""
    ordered_valid: list[UUID] = []
    seen: set[UUID] = set()
    stripped: list[str] = []

    def _add_valid(uid: UUID) -> None:
        if uid in allowed_record_ids and uid not in seen:
            seen.add(uid)
            ordered_valid.append(uid)

    def _clean_non_code_segment(segment: str) -> str:
        removed_in_seg = False

        def _replace_tag(match: re.Match[str]) -> str:
            nonlocal removed_in_seg
            raw_inner = match.group(1)
            uid = _try_parse_uuid(raw_inner)
            if uid is not None and uid in allowed_record_ids:
                _add_valid(uid)
                if strip_valid_tags:
                    removed_in_seg = True
                    return ""
                return f"[rec_{uid}]"
            stripped.append(match.group(0))
            removed_in_seg = True
            return ""

        cur = segment
        while True:
            nxt = _CITATION_TAG_RE.sub(_replace_tag, cur)
            if nxt == cur:
                break
            cur = nxt

        if removed_in_seg:
            cur = re.sub(r"[ \t]+([，。！？；：,.!?;:\]】])", r"\1", cur)
            cur = re.sub(r"([\[【])[ \t]+", r"\1", cur)
        cur = re.sub(r"[ \t]{2,}", " ", cur)
        return cur

    parts: list[str] = []
    last_end = 0
    for code_match in _CODE_SPAN_OR_FENCE_RE.finditer(text):
        start, end = code_match.span()
        if start > last_end:
            parts.append(_clean_non_code_segment(text[last_end:start]))
        parts.append(code_match.group(0))
        last_end = end
    if last_end < len(text):
        parts.append(_clean_non_code_segment(text[last_end:]))

    cleaned = "".join(parts).strip()

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
    """Incremental chunk processor handling nested brackets and fenced/inline code spans."""

    allowed_record_ids: set[UUID]
    strip_valid_tags: bool = False
    verified_record_ids: list[UUID] = field(default_factory=list)
    stripped_tags: list[str] = field(default_factory=list)
    _buffer: str = ""
    _seen: set[UUID] = field(default_factory=set)
    _in_code_fence: bool = False
    _fence_char: str = ""
    _fence_run: str = ""
    _in_inline_code: bool = False
    _inline_backtick_len: int = 0
    _inline_close_run: str = ""
    _pending_space: str = ""
    _just_stripped_tag: bool = False
    _last_emitted_char: str = ""

    def _emit_plain_char(self, ch: str, out: list[str]) -> None:
        if ch == " ":
            if self._just_stripped_tag:
                if self._pending_space:
                    return
                if self._last_emitted_char in ("[", "【", " ", ""):
                    return
            if self._pending_space:
                out.append(self._pending_space)
                self._last_emitted_char = " "
            self._pending_space = " "
            return

        if self._pending_space:
            if self._just_stripped_tag and ch in _CLOSING_OR_PUNCT_CHARS:
                self._pending_space = ""
            else:
                out.append(self._pending_space)
                self._last_emitted_char = " "
                self._pending_space = ""

        self._just_stripped_tag = False
        out.append(ch)
        if ch:
            self._last_emitted_char = ch[-1]

    def _emit_plain_str(self, s: str, out: list[str]) -> None:
        for ch in s:
            self._emit_plain_char(ch, out)

    def _process_outside_char(self, ch: str, out: list[str]) -> None:
        if self._buffer:
            if ch in ("[", "【"):
                prev = self._buffer
                self._buffer = ""
                self._emit_plain_str(prev, out)
                self._buffer = ch
                return

            if ch in ("\n", "\r") or len(self._buffer) >= 96:
                prev = self._buffer
                self._buffer = ""
                self._emit_plain_str(prev, out)
                self._process_outside_char(ch, out)
                return

            if ch in ("]", "】"):
                open_ch = self._buffer[0]
                expected_close = "]" if open_ch == "[" else "】"
                if ch != expected_close:
                    self._buffer += ch
                    return
                full_tag = self._buffer + ch
                inner = self._buffer[1:].strip()
                self._buffer = ""
                if inner.lower().startswith("rec_"):
                    uid = _try_parse_uuid(inner[4:])
                    if uid is not None and uid in self.allowed_record_ids:
                        if uid not in self._seen:
                            self._seen.add(uid)
                            self.verified_record_ids.append(uid)
                        if self.strip_valid_tags:
                            self._just_stripped_tag = True
                        else:
                            if self._pending_space:
                                out.append(self._pending_space)
                                self._pending_space = ""
                            self._just_stripped_tag = False
                            out.append(f"[rec_{uid}]")
                            self._last_emitted_char = "]"
                    else:
                        self.stripped_tags.append(full_tag)
                        self._just_stripped_tag = True
                else:
                    self._emit_plain_str(full_tag, out)
                return

            self._buffer += ch
            return

        if ch in ("[", "【"):
            self._buffer = ch
            return

        self._emit_plain_char(ch, out)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        out: list[str] = []

        for ch in chunk:
            if self._in_code_fence:
                if ch == self._fence_char:
                    self._fence_run += ch
                    if len(self._fence_run) == 3:
                        out.append(self._fence_run)
                        self._last_emitted_char = ch
                        self._fence_run = ""
                        self._in_code_fence = False
                        self._fence_char = ""
                else:
                    if self._fence_run:
                        out.append(self._fence_run)
                        self._fence_run = ""
                    out.append(ch)
                    self._last_emitted_char = ch
                continue

            if self._in_inline_code:
                if ch == "`":
                    self._inline_close_run += "`"
                    if len(self._inline_close_run) == self._inline_backtick_len:
                        out.append(self._inline_close_run)
                        self._last_emitted_char = "`"
                        self._inline_close_run = ""
                        self._in_inline_code = False
                        self._inline_backtick_len = 0
                else:
                    if self._inline_close_run:
                        out.append(self._inline_close_run)
                        self._inline_close_run = ""
                    if ch in ("\n", "\r"):
                        self._in_inline_code = False
                        self._inline_backtick_len = 0
                    out.append(ch)
                    self._last_emitted_char = ch
                continue

            if not self._buffer and ch in ("`", "~"):
                if not self._fence_run or self._fence_run[0] == ch:
                    self._fence_run += ch
                    if len(self._fence_run) == 3:
                        if self._pending_space:
                            out.append(self._pending_space)
                            self._pending_space = ""
                        self._just_stripped_tag = False
                        out.append(self._fence_run)
                        self._last_emitted_char = ch
                        self._in_code_fence = True
                        self._fence_char = ch
                        self._fence_run = ""
                    continue

                prev_run = self._fence_run
                self._fence_run = ""
                self._emit_plain_str(prev_run, out)
                self._fence_run = ch
                continue

            if self._fence_run:
                prev_run = self._fence_run
                self._fence_run = ""
                if prev_run[0] == "`":
                    if self._pending_space:
                        out.append(self._pending_space)
                        self._pending_space = ""
                    self._just_stripped_tag = False
                    out.append(prev_run)
                    self._last_emitted_char = "`"
                    if ch not in ("\n", "\r"):
                        self._in_inline_code = True
                        self._inline_backtick_len = len(prev_run)
                        self._inline_close_run = ""
                        out.append(ch)
                        self._last_emitted_char = ch
                        continue
                else:
                    self._emit_plain_str(prev_run, out)

            self._process_outside_char(ch, out)

        return "".join(out)

    def flush(self) -> str:
        out: list[str] = []

        if self._in_code_fence:
            if self._fence_run:
                out.append(self._fence_run)
                self._fence_run = ""
            self._in_code_fence = False
            self._fence_char = ""
            return "".join(out)

        if self._in_inline_code:
            if self._inline_close_run:
                out.append(self._inline_close_run)
                self._inline_close_run = ""
            self._in_inline_code = False
            self._inline_backtick_len = 0
            return "".join(out)

        if self._fence_run:
            prev_run = self._fence_run
            self._fence_run = ""
            self._emit_plain_str(prev_run, out)

        if self._buffer:
            rem = self._buffer
            self._buffer = ""
            if rem[1:].lstrip().lower().startswith("rec_"):
                self.stripped_tags.append(rem)
                self._just_stripped_tag = True
            else:
                self._emit_plain_str(rem, out)

        if self._pending_space:
            if not self._just_stripped_tag:
                out.append(self._pending_space)
            self._pending_space = ""

        self._just_stripped_tag = False
        return "".join(out)
