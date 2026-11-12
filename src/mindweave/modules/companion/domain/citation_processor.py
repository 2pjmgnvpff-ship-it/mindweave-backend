"""Streaming & batch `[rec_<id>]` citation verifier and scrubber (Invariants I4 & I6).

Pure Python domain module — zero framework, database, or vendor SDK imports.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

MarkerStyle = Literal["strip", "superscript", "footnote"]

_SUPERSCRIPT_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

_OPEN_BRACKETS = frozenset({"[", "【"})
_CLOSE_BRACKETS = frozenset({"]", "】"})
_MAX_TAG_BUFFER_LEN = 512

# Punctuation before which a stray ASCII space left by a stripped citation should be removed
_CLOSING_OR_CLAUSE_PUNCT = frozenset("，。！？；：、,.!?;:)）】》”’」』›»—…")

# Citation item prefix: rec_..., rec-..., rec:..., 记录#..., or bare 32/36-char hex UUID
_REC_PREFIX_RE = re.compile(
    r"^(?:rec[_:\-#]?|记录[#_:：]?)(?P<raw_id>[0-9a-zA-Z\-_]+)$",
    re.IGNORECASE,
)
_BARE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_UNCLOSED_REC_TAIL_RE = re.compile(
    r"^[\[【]\s*(?:rec[_:\-#]|记录[#_:：])[0-9a-zA-Z\-_,\s，、;；]*$",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[,，、;；\s]+")


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
    )


def _format_marker(index: int, style: MarkerStyle) -> str:
    if style == "superscript":
        return str(index).translate(_SUPERSCRIPT_DIGITS)
    if style == "footnote":
        return f"［{index}］"
    return ""


def _parse_uuid_safe(raw: str) -> UUID | None:
    cleaned = raw.strip()
    for prefix in ("rec_", "rec-", "rec:", "REC_", "REC-", "REC:", "记录#", "记录_", "记录:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    try:
        return UUID(cleaned)
    except (ValueError, AttributeError):
        return None


def _classify_bracket_tokens(inner: str) -> list[tuple[str, UUID | None]] | None:
    """Return list of `(raw_token, parsed_uuid_or_none)` if `inner` is a citation bracket, else `None`."""
    stripped = inner.strip()
    if not stripped:
        return None
    parts = [p for p in _TOKEN_SPLIT_RE.split(stripped) if p]
    if not parts:
        return None

    classified: list[tuple[str, UUID | None]] = []
    for part in parts:
        m = _REC_PREFIX_RE.match(part)
        if m is not None:
            classified.append((part, _parse_uuid_safe(m.group("raw_id"))))
            continue
        if _BARE_UUID_RE.match(part):
            classified.append((part, _parse_uuid_safe(part)))
            continue
        return None
    return classified


@dataclass(frozen=True, slots=True)
class VerifiedCitation:
    """Deduplicated 1-based citation verified against current `PersonScope` records."""

    index: int
    record_id: UUID


@dataclass(frozen=True, slots=True)
class CitationStepResult:
    """Incremental output emitted after feeding one streaming chunk or finalizing."""

    delta_text: str
    new_citations: tuple[VerifiedCitation, ...] = ()
    stripped_hallucinated_delta: int = 0


@dataclass(frozen=True, slots=True)
class CitationProcessSummary:
    """Complete post-processed result for non-streaming or finalized text."""

    cleaned_text: str
    citations: tuple[VerifiedCitation, ...]
    stripped_hallucinated_count: int


_CANDIDATE_TAG_FIRST_CHARS = frozenset("rR记0123456789abcdefABCDEF")


class StreamingCitationProcessor:
    """Chunk-boundary-invariant streaming citation processor (Invariants I4 & I6).

    - Buffers `[` / `【` tags across arbitrary SSE chunk splits.
    - Tracks Markdown triple-backtick (```` ``` ````) and tilde (`~~~`) code fences as well as
      inline single/double-backtick code spans (`` `[rec_...]` ``) across chunk splits so bracket
      literals inside code blocks/spans are never altered.
    - Handles nested square brackets (e.g. `[重点：忌口 [rec_...] 必看]`) cleanly.
    - Verifies every cited UUID against `allowed_record_ids`:
      - Valid (`id in allowed_record_ids`): strips raw `[rec_<uuid>]` internal tag (or replaces
        with clean superscript/footnote marker when configured) and emits deduplicated
        `VerifiedCitation(index, record_id)`.
      - Out-of-scope / hallucinated (`id not in allowed_record_ids` or malformed `rec_...`):
        strips the bogus tag completely and increments `stripped_hallucinated_count`.
    """

    def __init__(
        self,
        allowed_record_ids: Iterable[UUID],
        *,
        marker_style: MarkerStyle = "strip",
    ) -> None:
        self._allowed: frozenset[UUID] = frozenset(allowed_record_ids)
        self._marker_style: MarkerStyle = marker_style

        self._citation_map: dict[UUID, VerifiedCitation] = {}
        self._citations_ordered: list[VerifiedCitation] = []
        self._stripped_hallucinated_count: int = 0

        # Streaming state machine (character-exact for chunk-split invariance)
        self._in_code_fence: bool = False
        self._fence_char: str = ""
        self._fence_len: int = 0
        self._fence_saw_newline: bool = False
        self._fence_indent_count: int = 0
        self._fence_close_count: int = 0
        self._fence_trailing_ws: bool = False

        self._fence_run_buf: str = ""
        self._fence_run_at_line_start: bool = True
        self._at_line_start: bool = True
        self._line_indent: int = 0

        self._in_inline_code: bool = False
        self._inline_tick_len: int = 0
        self._inline_close_buf: str = ""
        self._tag_buf: str = ""
        self._pending_space: str = ""
        self._just_stripped_citation: bool = False
        self._last_emitted_char: str = ""

    @property
    def allowed_record_ids(self) -> frozenset[UUID]:
        return self._allowed

    @property
    def verified_citations(self) -> tuple[VerifiedCitation, ...]:
        return tuple(self._citations_ordered)

    @property
    def verified_record_ids(self) -> list[UUID]:
        return [c.record_id for c in self._citations_ordered]

    @property
    def stripped_hallucinated_count(self) -> int:
        return self._stripped_hallucinated_count

    def _record_valid_citation(
        self, record_id: UUID, step_new: list[VerifiedCitation]
    ) -> VerifiedCitation:
        existing = self._citation_map.get(record_id)
        if existing is not None:
            return existing
        created = VerifiedCitation(index=len(self._citations_ordered) + 1, record_id=record_id)
        self._citation_map[record_id] = created
        self._citations_ordered.append(created)
        step_new.append(created)
        return created

    def _emit_char(self, ch: str, out: list[str]) -> None:
        if self._pending_space:
            if self._just_stripped_citation:
                if (
                    ch in _CLOSING_OR_CLAUSE_PUNCT
                    or ch in (" ", "\t", "\r", "\n")
                    or (_is_cjk(self._last_emitted_char) and _is_cjk(ch))
                ):
                    self._pending_space = ""
                else:
                    out.append(self._pending_space)
                    self._last_emitted_char = self._pending_space[-1]
                    self._pending_space = ""
            else:
                out.append(self._pending_space)
                self._last_emitted_char = self._pending_space[-1]
                self._pending_space = ""
        elif self._just_stripped_citation and ch == " " and self._last_emitted_char == " ":
            # Collapse double spaces when space preceded `[rec_...]` and had already been emitted
            self._just_stripped_citation = False
            return

        self._just_stripped_citation = False
        out.append(ch)
        if ch:
            self._last_emitted_char = ch[-1]

    def _emit_plain_char(self, ch: str, out: list[str]) -> None:
        if ch == " ":
            if not self._pending_space:
                self._pending_space = " "
                return
            if self._just_stripped_citation:
                # Collapse spaces on both sides of a stripped citation while keeping holdback active
                return
        self._emit_char(ch, out)

    def _emit_plain_str(self, s: str, out: list[str]) -> None:
        for ch in s:
            self._emit_plain_char(ch, out)

    def _flush_pending_space_before_code(self, out: list[str]) -> None:
        if self._pending_space:
            if not self._just_stripped_citation:
                out.append(self._pending_space)
                self._last_emitted_char = self._pending_space[-1]
            self._pending_space = ""
        self._just_stripped_citation = False

    def _enter_code_fence(self, fence_char: str, fence_len: int) -> None:
        self._in_code_fence = True
        self._fence_char = fence_char
        self._fence_len = fence_len
        self._fence_saw_newline = False
        self._fence_indent_count = 0
        self._fence_close_count = 0
        self._fence_trailing_ws = False

    def _reset_code_fence(self) -> None:
        self._in_code_fence = False
        self._fence_char = ""
        self._fence_len = 0
        self._fence_saw_newline = False
        self._fence_indent_count = 0
        self._fence_close_count = 0
        self._fence_trailing_ws = False

    def _feed_fenced_char(
        self,
        ch: str,
        out: list[str],
        step_new: list[VerifiedCitation],
    ) -> int:
        if not self._fence_saw_newline:
            if self._fence_char == "`" and self._fence_close_count > 0 and ch != "`":
                if self._fence_close_count == self._fence_len:
                    # Single-line N-backtick inline code span closed right before `ch`
                    self._reset_code_fence()
                    self._at_line_start = False
                    return self._process_non_fenced_char(ch, out, step_new)
                self._fence_close_count = 0

            out.append(ch)
            self._last_emitted_char = ch
            if ch == "\n":
                self._fence_saw_newline = True
                self._fence_indent_count = 0
                self._fence_close_count = 0
                self._fence_trailing_ws = False
            elif self._fence_char == "`" and ch == "`":
                self._fence_close_count += 1
            return 0

        out.append(ch)
        self._last_emitted_char = ch

        if ch == "\n":
            if self._fence_indent_count >= 0 and self._fence_close_count >= self._fence_len:
                self._reset_code_fence()
                self._at_line_start = True
                self._line_indent = 0
            else:
                self._fence_indent_count = 0
                self._fence_close_count = 0
                self._fence_trailing_ws = False
            return 0

        if ch == "\r":
            return 0

        if (
            self._fence_indent_count >= 0
            and self._fence_close_count == 0
            and not self._fence_trailing_ws
        ):
            if ch in (" ", "\t"):
                self._fence_indent_count += 1
                if self._fence_indent_count > 3:
                    self._fence_indent_count = -1
            elif ch == self._fence_char:
                self._fence_close_count = 1
            else:
                self._fence_indent_count = -1
        elif (
            self._fence_indent_count >= 0
            and self._fence_close_count > 0
            and not self._fence_trailing_ws
        ):
            if ch == self._fence_char:
                self._fence_close_count += 1
            elif ch in (" ", "\t") and self._fence_close_count >= self._fence_len:
                self._fence_trailing_ws = True
            else:
                self._fence_indent_count = -1
                self._fence_close_count = 0
        elif self._fence_trailing_ws and ch not in (" ", "\t"):
            self._fence_indent_count = -1
            self._fence_close_count = 0
            self._fence_trailing_ws = False

        return 0

    def _feed_inline_code_char(self, ch: str, out: list[str]) -> None:
        if ch == "`":
            self._inline_close_buf += ch
            if len(self._inline_close_buf) == self._inline_tick_len:
                out.append(self._inline_close_buf)
                self._last_emitted_char = "`"
                self._inline_close_buf = ""
                self._in_inline_code = False
                self._inline_tick_len = 0
            return

        if self._inline_close_buf:
            out.append(self._inline_close_buf)
            self._last_emitted_char = "`"
            self._inline_close_buf = ""

        if ch in ("\n", "\r"):
            # Unclosed inline backticks do not span across line breaks
            self._in_inline_code = False
            self._inline_tick_len = 0
            out.append(ch)
            self._last_emitted_char = ch
            if ch == "\n":
                self._at_line_start = True
                self._line_indent = 0
            return

        out.append(ch)
        self._last_emitted_char = ch

    def _process_outside_fence_char(
        self,
        ch: str,
        out: list[str],
        step_new: list[VerifiedCitation],
    ) -> int:
        hallucinated_delta = 0

        # If currently buffering a `[` or `【` candidate tag:
        if self._tag_buf:
            if ch in _OPEN_BRACKETS:
                # Outer bracket was not a flat citation tag (e.g. "【重点：过敏 [rec_...]】")
                prev_tag = self._tag_buf
                self._tag_buf = ""
                self._emit_plain_str(prev_tag, out)
                self._tag_buf = ch
                return 0

            if ch in ("\n", "\r") or len(self._tag_buf) >= _MAX_TAG_BUFFER_LEN:
                prev_tag = self._tag_buf
                self._tag_buf = ""
                self._emit_plain_str(prev_tag, out)
                self._process_non_fenced_char(ch, out, step_new)
                return 0

            if ch in _CLOSE_BRACKETS:
                inner = self._tag_buf[1:]
                classified = _classify_bracket_tokens(inner)
                if classified is None:
                    raw_full = self._tag_buf + ch
                    self._tag_buf = ""
                    self._emit_plain_str(raw_full, out)
                    return 0

                self._tag_buf = ""
                valid_in_tag: list[VerifiedCitation] = []
                for _raw_tok, parsed_uuid in classified:
                    if parsed_uuid is not None and parsed_uuid in self._allowed:
                        vc = self._record_valid_citation(parsed_uuid, step_new)
                        valid_in_tag.append(vc)
                    else:
                        self._stripped_hallucinated_count += 1
                        hallucinated_delta += 1

                if valid_in_tag and self._marker_style != "strip":
                    # Drop space right before superscript/footnote marker
                    self._pending_space = ""
                    self._just_stripped_citation = False
                    markers = "".join(
                        _format_marker(vc.index, self._marker_style) for vc in valid_in_tag
                    )
                    out.append(markers)
                    self._last_emitted_char = markers[-1]
                else:
                    self._just_stripped_citation = True
                return hallucinated_delta

            # Early exit if first non-whitespace char inside `[...` cannot start a citation token
            if (
                not self._tag_buf[1:].strip()
                and not ch.isspace()
                and ch not in _CANDIDATE_TAG_FIRST_CHARS
            ):
                prev_tag = self._tag_buf
                self._tag_buf = ""
                self._emit_plain_str(prev_tag, out)
                return self._process_non_fenced_char(ch, out, step_new)

            self._tag_buf += ch
            return 0

        # Not inside `_tag_buf`: check for opening bracket or single horizontal space holdback
        if ch in _OPEN_BRACKETS:
            self._at_line_start = False
            self._tag_buf = ch
            return 0

        if ch == "\n":
            self._at_line_start = True
            self._line_indent = 0
        elif ch in (" ", "\t") and self._at_line_start:
            self._line_indent += 1
            if self._line_indent > 3:
                self._at_line_start = False
        elif ch != "\r":
            self._at_line_start = False

        self._emit_plain_char(ch, out)
        return 0

    def _resolve_fence_run_before_char(
        self,
        ch: str,
        out: list[str],
        step_new: list[VerifiedCitation],
    ) -> tuple[bool, int]:
        run = self._fence_run_buf
        run_at_line_start = self._fence_run_at_line_start
        self._fence_run_buf = ""
        run_char = run[0]
        run_len = len(run)

        if run_len >= 3 and (run_char == "`" or run_at_line_start):
            self._flush_pending_space_before_code(out)
            out.append(run)
            self._last_emitted_char = run_char
            self._enter_code_fence(run_char, run_len)
            return True, self._feed_fenced_char(ch, out, step_new)

        if run_char == "`":
            self._flush_pending_space_before_code(out)
            out.append(run)
            self._last_emitted_char = "`"
            if ch in ("\n", "\r"):
                out.append(ch)
                self._last_emitted_char = ch
                if ch == "\n":
                    self._at_line_start = True
                    self._line_indent = 0
            else:
                self._in_inline_code = True
                self._inline_tick_len = run_len
                self._inline_close_buf = ""
                self._feed_inline_code_char(ch, out)
            return True, 0

        delta = 0
        for c in run:
            delta += self._process_outside_fence_char(c, out, step_new)
        return False, delta

    def _process_non_fenced_char(
        self,
        ch: str,
        out: list[str],
        step_new: list[VerifiedCitation],
    ) -> int:
        hallucinated_delta = 0

        if not self._tag_buf and ch in ("`", "~"):
            if not self._fence_run_buf:
                self._fence_run_at_line_start = self._at_line_start
                self._at_line_start = False
                self._fence_run_buf = ch
                return 0
            if self._fence_run_buf[0] == ch:
                self._fence_run_buf += ch
                return 0

            handled, delta = self._resolve_fence_run_before_char(ch, out, step_new)
            hallucinated_delta += delta
            if handled:
                return hallucinated_delta
            # Previous tilde run was ordinary text; start buffering the new delimiter `ch`
            self._fence_run_at_line_start = self._at_line_start
            self._at_line_start = False
            self._fence_run_buf = ch
            return hallucinated_delta

        if self._fence_run_buf:
            handled, delta = self._resolve_fence_run_before_char(ch, out, step_new)
            hallucinated_delta += delta
            if handled:
                return hallucinated_delta

        hallucinated_delta += self._process_outside_fence_char(ch, out, step_new)
        return hallucinated_delta

    def feed(self, chunk: str) -> CitationStepResult:
        if not chunk:
            return CitationStepResult(delta_text="")

        out: list[str] = []
        step_new: list[VerifiedCitation] = []
        hallucinated_delta = 0

        for ch in chunk:
            if self._in_code_fence:
                hallucinated_delta += self._feed_fenced_char(ch, out, step_new)
                continue

            if self._in_inline_code:
                self._feed_inline_code_char(ch, out)
                continue

            hallucinated_delta += self._process_non_fenced_char(ch, out, step_new)

        return CitationStepResult(
            delta_text="".join(out),
            new_citations=tuple(step_new),
            stripped_hallucinated_delta=hallucinated_delta,
        )

    def finalize(self) -> CitationStepResult:
        out: list[str] = []
        step_new: list[VerifiedCitation] = []
        hallucinated_delta = 0

        if self._in_code_fence:
            self._reset_code_fence()
        elif self._in_inline_code:
            if self._inline_close_buf:
                out.append(self._inline_close_buf)
                self._inline_close_buf = ""
            self._in_inline_code = False
            self._inline_tick_len = 0
        else:
            if self._fence_run_buf:
                run = self._fence_run_buf
                run_at_line_start = self._fence_run_at_line_start
                self._fence_run_buf = ""
                if run[0] == "`" or (len(run) >= 3 and run_at_line_start):
                    self._flush_pending_space_before_code(out)
                    out.append(run)
                    self._last_emitted_char = run[-1]
                else:
                    for c in run:
                        hallucinated_delta += self._process_outside_fence_char(c, out, step_new)

            if self._tag_buf:
                tail = self._tag_buf
                self._tag_buf = ""
                if _UNCLOSED_REC_TAIL_RE.match(tail):
                    # Truncated `[rec_...` at EOF: strip completely (Invariant I6)
                    self._stripped_hallucinated_count += 1
                    hallucinated_delta += 1
                    self._just_stripped_citation = True
                else:
                    self._emit_plain_str(tail, out)

            if self._pending_space:
                if not self._just_stripped_citation:
                    out.append(self._pending_space)
                self._pending_space = ""

            self._just_stripped_citation = False

        return CitationStepResult(
            delta_text="".join(out),
            new_citations=tuple(step_new),
            stripped_hallucinated_delta=hallucinated_delta,
        )

    def merge_declared_citations(
        self,
        cited_record_ids: Sequence[UUID | str],
    ) -> list[VerifiedCitation]:
        """Validate non-streaming `CompanionChatOutput.cited_record_ids` and merge valid ones."""
        newly_added: list[VerifiedCitation] = []
        for raw_id in cited_record_ids:
            parsed = raw_id if isinstance(raw_id, UUID) else _parse_uuid_safe(str(raw_id))
            if parsed is not None and parsed in self._allowed:
                self._record_valid_citation(parsed, newly_added)
            else:
                self._stripped_hallucinated_count += 1
        return newly_added


def validate_cited_record_ids(
    cited_record_ids: Sequence[UUID | str],
    allowed_record_ids: Iterable[UUID],
) -> tuple[list[UUID], int]:
    """Filter `cited_record_ids` strictly to `allowed_record_ids` (deduplicated, order-preserving).

    Returns `(verified_record_ids, stripped_hallucinated_count)`.
    """
    allowed = frozenset(allowed_record_ids)
    verified: list[UUID] = []
    seen: set[UUID] = set()
    stripped = 0
    for raw_id in cited_record_ids:
        parsed = raw_id if isinstance(raw_id, UUID) else _parse_uuid_safe(str(raw_id))
        if parsed is not None and parsed in allowed:
            if parsed not in seen:
                seen.add(parsed)
                verified.append(parsed)
        else:
            stripped += 1
    return verified, stripped


def process_complete_citations(
    text: str,
    allowed_record_ids: Iterable[UUID],
    *,
    declared_cited_ids: Sequence[UUID | str] = (),
    marker_style: MarkerStyle = "strip",
) -> CitationProcessSummary:
    """Process a complete reply string + optional declared `cited_record_ids`."""
    proc = StreamingCitationProcessor(allowed_record_ids, marker_style=marker_style)
    step1 = proc.feed(text)
    step2 = proc.finalize()
    if declared_cited_ids:
        proc.merge_declared_citations(declared_cited_ids)
    return CitationProcessSummary(
        cleaned_text=step1.delta_text + step2.delta_text,
        citations=proc.verified_citations,
        stripped_hallucinated_count=proc.stripped_hallucinated_count,
    )
