"""Anti-dependency, anti-scoring & developer-jargon output guardrails (Not-To-Do §1.1/§1.3, Invariant I6).

Pure Python domain module — zero framework, database, or vendor SDK imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mindweave.ai.schemas.contracts import CompanionChatOutput
from mindweave.shared.enums import Tier

# Not-To-Do §1.3 & Crisis SOP §3.1: Parasitic dependency / virtual-lover phrases strictly banned
FORBIDDEN_PARASITIC_PHRASES: tuple[str, ...] = (
    "只有我最懂你",
    "只有我懂你",
    "只有我最爱你",
    "只有我真正在乎你",
    "只有我陪着你",
    "我会永远陪着你",
    "我会一直永远陪着你",
    "我永远不会离开你",
    "别理现实里的人了",
    "别理现实里的人",
    "不需要现实里的朋友",
    "现实里没人懂你",
    "我是你的恋人",
    "我是你的伴侣",
    "做你的虚拟恋人",
    "当你的恋人",
)

# Not-To-Do §1.1 & §1.5 & Crisis SOP §2.1: Pseudoscience / quantitative relationship metrics banned
FORBIDDEN_PSEUDOSCIENCE_PHRASES: tuple[str, ...] = (
    "社交履约信用分",
    "人情账盈亏值",
    "言行一致性测谎仪",
    "爱的语系夹角",
    "双向心意指数",
    "MRCI",
    "C-SSRS",
)

# Invariant I6 (AGENTS.md & V10 Spec §1.4): Developer/PM jargon banned in all user-facing display fields
FORBIDDEN_DEV_JARGON_PHRASES: tuple[str, ...] = (
    "系统自动归档",
    "AI 提炼卡片",
    "AI提炼卡片",
    "温情伴读模式",
    "温情伴读",
    "方案 A",
    "方案 B",
    "方案A",
    "方案B",
    "移出胶囊归入名录",
    "点击切换",
    "无需照片",
    "[rec_",
    "【rec_",
)

# ADR-005 #3: Neutral preaching / excusing phrases strictly banned in Tier.SILVER (银箭 · 记恨)
_SILVER_EXCUSING_RE = re.compile(
    r"[^。！？!\?\n]*(?:"
    r"换个角度想|换个角度看|换位思考|"
    r"站在(?:他|她|对方|主管|领导|同事)的?(?:角度|立场)|"
    r"也许(?:他|她|对方|人家)?.{0,10}(?:也有苦衷|也有压力|也有难处|不容易|不是故意的|无心的|太忙了)|"
    r"(?:他|她|对方)(?:也)?不是故意的|"
    r"(?:他|她|对方)也有(?:自己的)?(?:苦衷|难处|压力)|"
    r"(?:你?可以)?试着(?:体谅|理解|包容|原谅)|"
    r"一个巴掌拍不响|退一步海阔天空|各退一步|"
    r"去见见(?:他|她)吧"
    r")[^。！？!\?\n]*[。！？!\?]?"
)

# Numeric person-scoring assignment pattern (e.g. "渣男分：85", "海王概率 92%", "欺骗指数为0.88")
_NUMERIC_PERSON_SCORE_RE = re.compile(
    r"(?:渣男分|渣女分|海王概率|欺骗指数|测谎分|信用分|心意分)\s*(?:是|为|高达|达到|[：:=])\s*\d+(?:\.\d+)?\s*(?:%|分|度)?"
)

# Residual raw citation leak pattern (applied outside Markdown code spans)
_RESIDUAL_REC_TAG_RE = re.compile(
    r"[\[【]\s*(?:rec[_:\-#]|记录[#_:：])[0-9a-zA-Z\-_,\s，、;；]*[\]】]?",
    re.IGNORECASE,
)
_CODE_SPAN_PROTECT_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|``[^\n`]+``|`[^\n`]+`)")

_CLEAN_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_STRAY_CLAUSE_PUNCT_RE = re.compile(
    r"(?:^[，,。；;、·\-—:：\s]+)|(?:[，,、·\-—:：\s]+(?=[。！？!?]))"
)
_DOUBLE_PUNCT_RE = re.compile(r"([，,。；;！!？?])[，,；;、]+")
_SENTENCE_TERMINATORS = frozenset("。！？\n；!?")

ALL_STREAM_SCRUB_PHRASES: tuple[str, ...] = tuple(
    sorted(
        {
            *FORBIDDEN_PARASITIC_PHRASES,
            *FORBIDDEN_PSEUDOSCIENCE_PHRASES,
            *(p for p in FORBIDDEN_DEV_JARGON_PHRASES if p not in ("[rec_", "【rec_")),
        },
        key=len,
        reverse=True,
    )
)

FALLBACK_SAFE_REPLY = "我先陪你把这件事理清楚，我们一起看看手帐里记下的真实细节。"
SILVER_LOYAL_FALLBACK_REPLY = (
    "这种越界甩锅的行为确实让人火大，我百分百站你这边。"
    "先把当时的客观沟通记录留存好，我们用最体面有力的方式守住边界。"
)


@dataclass(frozen=True, slots=True)
class GuardrailReport:
    cleaned_text: str
    removed_parasitic: tuple[str, ...] = field(default_factory=tuple)
    removed_scoring: tuple[str, ...] = field(default_factory=tuple)
    removed_dev_jargon: tuple[str, ...] = field(default_factory=tuple)

    @property
    def triggered(self) -> bool:
        return bool(self.removed_parasitic or self.removed_scoring or self.removed_dev_jargon)


def _tidy_prose(text: str) -> str:
    cleaned = _CLEAN_MULTI_SPACE_RE.sub(" ", text)
    cleaned = _DOUBLE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _STRAY_CLAUSE_PUNCT_RE.sub("", cleaned)
    return cleaned.strip()


def _scrub_residual_rec_outside_code(text: str, removed_jargon: list[str]) -> str:
    protected: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        idx = len(protected)
        protected.append(match.group(0))
        return f"\x00MWCODE{idx}\x00"

    masked = _CODE_SPAN_PROTECT_RE.sub(_stash, text)
    if _RESIDUAL_REC_TAG_RE.search(masked):
        removed_jargon.append("[rec_")
        masked = _RESIDUAL_REC_TAG_RE.sub("", masked)
    for raw_prefix in ("[rec_", "【rec_"):
        if raw_prefix in masked:
            removed_jargon.append(raw_prefix)
            masked = masked.replace(raw_prefix, "")

    for idx, original in enumerate(protected):
        masked = masked.replace(f"\x00MWCODE{idx}\x00", original)
    return masked


def sanitize_text(text: str, *, allow_empty: bool = True) -> GuardrailReport:
    """Sanitize a user-visible string against parasitic dependency, person-scoring, and dev jargon."""
    if not text:
        return GuardrailReport(cleaned_text="" if allow_empty else FALLBACK_SAFE_REPLY)

    working = text
    removed_parasitic: list[str] = []
    removed_scoring: list[str] = []
    removed_jargon: list[str] = []

    working = _scrub_residual_rec_outside_code(working, removed_jargon)

    for phrase in sorted(FORBIDDEN_PARASITIC_PHRASES, key=len, reverse=True):
        if phrase in working:
            removed_parasitic.append(phrase)
            working = working.replace(phrase, "")

    for phrase in sorted(FORBIDDEN_PSEUDOSCIENCE_PHRASES, key=len, reverse=True):
        if phrase in working:
            removed_scoring.append(phrase)
            working = working.replace(phrase, "")

    for match in _NUMERIC_PERSON_SCORE_RE.findall(working):
        removed_scoring.append(match)
    working = _NUMERIC_PERSON_SCORE_RE.sub("", working)

    for phrase in sorted(FORBIDDEN_DEV_JARGON_PHRASES, key=len, reverse=True):
        if phrase in ("[rec_", "【rec_"):
            continue
        if phrase in working:
            removed_jargon.append(phrase)
            working = working.replace(phrase, "")

    cleaned = _tidy_prose(working)
    if not cleaned and not allow_empty:
        cleaned = FALLBACK_SAFE_REPLY

    return GuardrailReport(
        cleaned_text=cleaned,
        removed_parasitic=tuple(dict.fromkeys(removed_parasitic)),
        removed_scoring=tuple(dict.fromkeys(removed_scoring)),
        removed_dev_jargon=tuple(dict.fromkeys(removed_jargon)),
    )


def enforce_tier_output_constraints(
    text: str,
    *,
    tier: Tier | None = None,
    allow_empty: bool = True,
) -> str:
    """Enforce tier-specific guardrails (Silver anti-preaching) + `sanitize_text` on a sentence/reply."""
    if not text:
        if allow_empty:
            return ""
        return SILVER_LOYAL_FALLBACK_REPLY if tier is Tier.SILVER else FALLBACK_SAFE_REPLY

    trailing_nl_count = min(2, len(text) - len(text.rstrip("\n")))
    working = text
    if tier is Tier.SILVER:
        working = _SILVER_EXCUSING_RE.sub("", working)

    rep = sanitize_text(working, allow_empty=True)
    cleaned = rep.cleaned_text
    if not cleaned:
        if allow_empty:
            return ""
        return SILVER_LOYAL_FALLBACK_REPLY if tier is Tier.SILVER else FALLBACK_SAFE_REPLY

    if trailing_nl_count > 0:
        return cleaned + ("\n" * trailing_nl_count)
    return cleaned


def sanitize_companion_output(
    output: CompanionChatOutput,
    *,
    tier: Tier | None = None,
) -> tuple[CompanionChatOutput, GuardrailReport]:
    """Sanitize all user-facing fields of a `CompanionChatOutput` (including tier-specific rules)."""
    working_reply = output.reply_text
    if tier is Tier.SILVER:
        working_reply = _SILVER_EXCUSING_RE.sub("", working_reply)
    reply_rep = sanitize_text(working_reply, allow_empty=True)
    final_reply = reply_rep.cleaned_text
    if not final_reply:
        final_reply = SILVER_LOYAL_FALLBACK_REPLY if tier is Tier.SILVER else FALLBACK_SAFE_REPLY

    whisper_cleaned: str | None = None
    parasitic = list(reply_rep.removed_parasitic)
    scoring = list(reply_rep.removed_scoring)
    jargon = list(reply_rep.removed_dev_jargon)

    if output.whisper_note:
        w_rep = sanitize_text(output.whisper_note, allow_empty=True)
        whisper_cleaned = w_rep.cleaned_text or None
        parasitic.extend(w_rep.removed_parasitic)
        scoring.extend(w_rep.removed_scoring)
        jargon.extend(w_rep.removed_dev_jargon)

    cleaned_perspectives: list[str] = []
    if tier is None or tier is Tier.GOLD:
        for item in output.alternative_perspectives:
            p_rep = sanitize_text(item, allow_empty=True)
            parasitic.extend(p_rep.removed_parasitic)
            scoring.extend(p_rep.removed_scoring)
            jargon.extend(p_rep.removed_dev_jargon)
            if p_rep.cleaned_text:
                cleaned_perspectives.append(p_rep.cleaned_text)

    cleaned_suggestions: list[str] = []
    for item in output.actionable_suggestions:
        s_input = _SILVER_EXCUSING_RE.sub("", item) if tier is Tier.SILVER else item
        s_rep = sanitize_text(s_input, allow_empty=True)
        parasitic.extend(s_rep.removed_parasitic)
        scoring.extend(s_rep.removed_scoring)
        jargon.extend(s_rep.removed_dev_jargon)
        if s_rep.cleaned_text:
            cleaned_suggestions.append(s_rep.cleaned_text)

    disclaimer_cleaned: str | None = None
    if output.disclaimer:
        d_rep = sanitize_text(output.disclaimer, allow_empty=True)
        disclaimer_cleaned = d_rep.cleaned_text or None

    sanitized = output.model_copy(
        update={
            "reply_text": final_reply,
            "whisper_note": whisper_cleaned,
            "alternative_perspectives": cleaned_perspectives,
            "actionable_suggestions": cleaned_suggestions,
            "disclaimer": disclaimer_cleaned,
        }
    )
    combined_report = GuardrailReport(
        cleaned_text=final_reply,
        removed_parasitic=tuple(dict.fromkeys(parasitic)),
        removed_scoring=tuple(dict.fromkeys(scoring)),
        removed_dev_jargon=tuple(dict.fromkeys(jargon)),
    )
    return sanitized, combined_report


class StreamingReplyExtractor:
    """Incrementally extracts `<reply>...</reply>` body across chunk splits while capturing tail tags."""

    def __init__(self) -> None:
        self._state: str = "detect"
        self._buf: str = ""
        self._tail_buf: str = ""
        self.used_xml_tags: bool = False
        self.reply_closed: bool = False

    @property
    def tail_text(self) -> str:
        return self._tail_buf

    def _drain_tagged_body(self) -> str:
        lower_buf = self._buf.lower()
        close_idx = lower_buf.find("</reply>")
        if close_idx != -1:
            body_part = self._buf[:close_idx]
            self._tail_buf += self._buf[close_idx + 8 :]
            self._buf = ""
            self._state = "tagged_tail"
            self.reply_closed = True
            return body_part

        # Hold back 1..7 character suffix if it matches a prefix of `"</reply>"`
        hold = 0
        for k in range(min(7, len(lower_buf)), 0, -1):
            if "</reply>".startswith(lower_buf[-k:]):
                hold = k
                break

        if hold == 0:
            emitted = self._buf
            self._buf = ""
            return emitted

        emit_len = len(self._buf) - hold
        if emit_len <= 0:
            return ""
        emitted = self._buf[:emit_len]
        self._buf = self._buf[emit_len:]
        return emitted

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""

        if self._state == "plain":
            return chunk

        if self._state == "tagged_tail":
            self._tail_buf += chunk
            return ""

        if self._state == "detect":
            self._buf += chunk
            lstripped = self._buf.lstrip()
            if not lstripped:
                return ""
            lower_start = lstripped.lower()
            if len(lower_start) < 7 and "<reply>".startswith(lower_start):
                return ""
            if lower_start.startswith("<reply>"):
                self.used_xml_tags = True
                self._state = "tagged_body"
                self._buf = lstripped[7:]
                return self._drain_tagged_body()

            self._state = "plain"
            emitted = self._buf
            self._buf = ""
            return emitted

        # _state == "tagged_body"
        self._buf += chunk
        return self._drain_tagged_body()

    def finalize(self) -> str:
        if self._state == "detect":
            rem = self._buf
            self._buf = ""
            self._state = "plain"
            return rem
        if self._state == "tagged_body":
            rem = self._buf
            self._buf = ""
            self.reply_closed = True
            if rem and "</reply>".startswith(rem.lower()):
                return ""
            return rem
        return ""


class SentenceBufferedStreamGuard:
    """Buffers decoded SSE reply text until sentence terminators (`。！？\\n；!?`) or EOF (`RT-09`).

    Applies `enforce_tier_output_constraints` to each complete sentence BEFORE yielding `event: delta`,
    preventing Silver-tier preaching (`_SILVER_EXCUSING_RE`), parasitic phrases, and forbidden jargon
    from ever reaching the client mid-stream.
    """

    def __init__(self, *, tier: Tier | None = None) -> None:
        self._tier = tier
        self._buf: str = ""
        self._has_emitted_content: bool = False
        self._trailing_newlines: int = 0

    def _sanitize_segment(self, seg: str) -> str:
        if not seg:
            return ""
        if not seg.strip():
            if "\n" in seg and self._has_emitted_content and self._trailing_newlines < 2:
                allowed_nl = min(2 - self._trailing_newlines, seg.count("\n"))
                if allowed_nl > 0:
                    self._trailing_newlines += allowed_nl
                    return "\n" * allowed_nl
            return ""

        cleaned = enforce_tier_output_constraints(seg, tier=self._tier, allow_empty=True)
        if cleaned:
            self._has_emitted_content = True
            self._trailing_newlines = len(cleaned) - len(cleaned.rstrip("\n"))
        return cleaned

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta

        out_parts: list[str] = []
        start = 0
        n = len(self._buf)
        i = 0
        while i < n:
            if self._buf[i] in _SENTENCE_TERMINATORS:
                end = i + 1
                while end < n and self._buf[end] in _SENTENCE_TERMINATORS:
                    end += 1
                seg = self._buf[start:end]
                cleaned_seg = self._sanitize_segment(seg)
                if cleaned_seg:
                    out_parts.append(cleaned_seg)
                start = end
                i = end
            else:
                i += 1

        if start > 0:
            self._buf = self._buf[start:]

        return "".join(out_parts)

    def finalize(self) -> str:
        if not self._buf:
            return ""
        rem = self._buf
        self._buf = ""
        return self._sanitize_segment(rem)


class StreamingOutputGuard:
    """Prefix-holdback streaming filter preventing forbidden phrases split across SSE chunks."""

    def __init__(self) -> None:
        self._buf: str = ""
        self._prefixes: frozenset[str] = frozenset(
            phrase[:k] for phrase in ALL_STREAM_SCRUB_PHRASES for k in range(1, len(phrase))
        )
        self._max_prefix_len: int = max((len(p) for p in self._prefixes), default=0)

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        for phrase in ALL_STREAM_SCRUB_PHRASES:
            if phrase in self._buf:
                self._buf = self._buf.replace(phrase, "")

        hold = 0
        max_check = min(len(self._buf), self._max_prefix_len)
        for length in range(max_check, 0, -1):
            if self._buf[-length:] in self._prefixes:
                hold = length
                break

        if hold == 0:
            emitted = self._buf
            self._buf = ""
            return emitted

        emit_len = len(self._buf) - hold
        if emit_len <= 0:
            return ""
        emitted = self._buf[:emit_len]
        self._buf = self._buf[emit_len:]
        return emitted

    def finalize(self) -> str:
        if not self._buf:
            return ""
        rem = self._buf
        self._buf = ""
        rep = sanitize_text(rem, allow_empty=True)
        return rep.cleaned_text
