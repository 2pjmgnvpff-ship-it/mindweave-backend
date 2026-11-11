"""Anti-dependency, anti-scoring & developer-jargon output guardrails (Not-To-Do §1.1/§1.3, Invariant I6).

Pure Python domain module — zero framework, database, or vendor SDK imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mindweave.ai.schemas.contracts import CompanionChatOutput

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

# Numeric person-scoring assignment pattern (e.g. "渣男分：85", "海王概率 92%", "欺骗指数为0.88")
_NUMERIC_PERSON_SCORE_RE = re.compile(
    r"(?:渣男分|渣女分|海王概率|欺骗指数|测谎分|信用分|心意分)\s*(?:是|为|高达|达到|[：:=])\s*\d+(?:\.\d+)?\s*(?:%|分|度)?"
)

# Residual raw citation leak pattern
_RESIDUAL_REC_TAG_RE = re.compile(
    r"[\[【]\s*(?:rec[_:\-#]|记录[#_:：])[0-9a-zA-Z\-_,\s，、;；]*[\]】]?",
    re.IGNORECASE,
)

_CLEAN_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_STRAY_CLAUSE_PUNCT_RE = re.compile(
    r"(?:^[，,。；;、·\-—:：\s]+)|(?:[，,、·\-—:：\s]+(?=[。！？!?]))"
)
_DOUBLE_PUNCT_RE = re.compile(r"([，,。；;！!？?])[，,；;、]+")

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


def sanitize_text(text: str, *, allow_empty: bool = True) -> GuardrailReport:
    """Sanitize a user-visible string against parasitic dependency, person-scoring, and dev jargon."""
    if not text:
        return GuardrailReport(cleaned_text="" if allow_empty else FALLBACK_SAFE_REPLY)

    working = text
    removed_parasitic: list[str] = []
    removed_scoring: list[str] = []
    removed_jargon: list[str] = []

    if _RESIDUAL_REC_TAG_RE.search(working):
        removed_jargon.append("[rec_")
        working = _RESIDUAL_REC_TAG_RE.sub("", working)

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
            if phrase in working:
                removed_jargon.append(phrase)
                working = working.replace(phrase, "")
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


def sanitize_companion_output(
    output: CompanionChatOutput,
) -> tuple[CompanionChatOutput, GuardrailReport]:
    """Sanitize all user-facing fields of a `CompanionChatOutput`."""
    reply_rep = sanitize_text(output.reply_text, allow_empty=False)

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
    for item in output.alternative_perspectives:
        p_rep = sanitize_text(item, allow_empty=True)
        parasitic.extend(p_rep.removed_parasitic)
        scoring.extend(p_rep.removed_scoring)
        jargon.extend(p_rep.removed_dev_jargon)
        if p_rep.cleaned_text:
            cleaned_perspectives.append(p_rep.cleaned_text)

    cleaned_suggestions: list[str] = []
    for item in output.actionable_suggestions:
        s_rep = sanitize_text(item, allow_empty=True)
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
            "reply_text": reply_rep.cleaned_text,
            "whisper_note": whisper_cleaned,
            "alternative_perspectives": cleaned_perspectives,
            "actionable_suggestions": cleaned_suggestions,
            "disclaimer": disclaimer_cleaned,
        }
    )
    combined_report = GuardrailReport(
        cleaned_text=reply_rep.cleaned_text,
        removed_parasitic=tuple(dict.fromkeys(parasitic)),
        removed_scoring=tuple(dict.fromkeys(scoring)),
        removed_dev_jargon=tuple(dict.fromkeys(jargon)),
    )
    return sanitized, combined_report


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
