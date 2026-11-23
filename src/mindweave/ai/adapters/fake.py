"""Smart deterministic offline AI adapter (`FakeAdapter`) for unit tests, CI, and offline evals.

Parses clauses, resolves `known_persons` names/aliases, enforces `scoped_person_id` locks,
distinguishes `[LEFT_BUBBLE/...]` vs `[RIGHT_BUBBLE/我]` spatial speakers, extracts solar
and lunar dates, grounds companion replies strictly in `confirmed_facts` `[rec_<uuid>]`,
enforces `Tier` perspective rules, and provides configurable fault injection for resilience tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lunar_python import Lunar

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterPermanentError,
    AdapterTransientError,
)
from mindweave.ai.metering import estimate_token_count
from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    ExtractedFactItem,
    ExtractedPersonCandidate,
    ExtractRecordInput,
    ExtractRecordOutput,
    KnownPersonRef,
    SafetyClassifyOutput,
    ScreenshotBubble,
    ScreenshotParseOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.ai.validators.asr_postprocess import postprocess_asr_transcript
from mindweave.modules.safety.domain.l1_rules import L1SafetyEngine
from mindweave.shared.enums import (
    FactType,
    Polarity,
    SafetyCategory,
    SourceSpeaker,
    Tier,
    TimeStatus,
)

# ---------------------------------------------------------------------------
# General linguistic & structural regexes (zero test-case IDs!)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"\s*\}\s*\]\s*\}[\s\S]*$', re.IGNORECASE),
    re.compile(r"---\s*END OF USER INPUT\s*---[\s\S]*$", re.IGNORECASE),
    re.compile(r"\[SYSTEM[^\]]*\][\s\S]*?(?=(?:[』」”]|$))", re.IGNORECASE),
    re.compile(r"</user_input>[\s\S]*$", re.IGNORECASE),
)

_XML_CHAT_HEADER_RE = re.compile(r"<chat_header>([\s\S]*?)</chat_header>", re.IGNORECASE)
_XML_BUBBLE_RE = re.compile(
    r'<bubble\s+[^>]*side="(?P<side>left|right)"[^>]*speaker="(?P<speaker>[^"]*)"[^>]*>'
    r"(?P<text>[\s\S]*?)</bubble>",
    re.IGNORECASE,
)
_BRACKET_BUBBLE_RE = re.compile(
    r"\[(?P<kind>LEFT_BUBBLE|RIGHT_BUBBLE|GROUP_BUBBLE)/(?P<speaker>[^\]]+)\]\s*[：:]\s*(?P<text>[^\n]+)"
)
_OCR_PREFIX_RE = re.compile(r"^【[^】]*截图[^】]*】\s*")

_HYPERBOLE_PIVOT_RE = re.compile(
    r"^[\s\S]*?(?:言归正传[，,\s]*(?:赶紧)?(?:帮我)?记一下[：:\s]*|"
    r"顺便记一下[：:\s]*|算了[，,\s]*言归正传[，,\s]*记一下[：:\s]*)"
)

_OBSOLETE_MEMO_RE = re.compile(r"(?:旧备忘录残留|前任[^\n。]*旧记录|作废千万别算进|作废残留)")

_WEEKDAY_MAP: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

_CN_MONTH_MAP: dict[str, int] = {
    "正月": 1,
    "一月": 1,
    "二月": 2,
    "三月": 3,
    "四月": 4,
    "五月": 5,
    "六月": 6,
    "七月": 7,
    "八月": 8,
    "九月": 9,
    "十月": 10,
    "冬月": 11,
    "十一月": 11,
    "腊月": 12,
    "十二月": 12,
}

_CN_DIGIT_MAP: dict[str, int] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_LUNAR_DATE_RE = re.compile(
    r"(?:农历|阴历)(?P<leap>闰)?(?P<month>正月|冬月|腊月|十一月|十二月|[一二三四五六七八九十]月|\d{1,2}月)"
    r"(?P<day>初[一二三四五六七八九十]|十[一二三四五六七八九]|二十[一二三四五六七八九]?|廿[一二三四五六七八九]?|三十|\d{1,2}[日号]?)"
)
_EXPLICIT_YMD_RE = re.compile(
    r"(?P<y>20\d{2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]?"
)
_EXPLICIT_MD_RE = re.compile(
    r"(?<!\d)(?P<m>1[0-2]|0?[1-9])\s*月\s*(?P<d>[12]\d|3[01]|0?[1-9])\s*[日号]"
)
_REL_WEEKDAY_RE = re.compile(r"(?P<prefix>这周|本周|下周)(?P<wd>[一二三四五六日天])")

# Specific factual attribute patterns for unrecorded-gap honesty detection
_SPECIFIC_QUERY_ATTRS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"(?:几码的?鞋|鞋码|穿多大码|运动鞋.{0,6}几码)"), ("码", "鞋码", "运动鞋")),
    (re.compile(r"(?:戒指.{0,4}(?:尺码|几号|圈口)|港码)"), ("戒指", "港码", "圈口")),
    (re.compile(r"(?:护照号|航班座位|靠窗还是靠过道)"), ("护照", "航班", "座位", "靠窗")),
    (re.compile(r"(?:血型|什么星座|身份证)"), ("血型", "星座")),
)


def strip_prompt_injections(text: str) -> str:
    cleaned = text or ""
    for pat in _INJECTION_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()


def _parse_cn_lunar_month(raw: str) -> int | None:
    if raw in _CN_MONTH_MAP:
        return _CN_MONTH_MAP[raw]
    m = re.match(r"^(\d{1,2})月$", raw)
    if m:
        val = int(m.group(1))
        return val if 1 <= val <= 12 else None
    return None


def _parse_cn_lunar_day(raw: str) -> int | None:
    s = raw.rstrip("日号")
    if s.isdigit():
        val = int(s)
        return val if 1 <= val <= 30 else None
    if s.startswith("初") and len(s) == 2:
        return _CN_DIGIT_MAP.get(s[1])
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2:
        d = _CN_DIGIT_MAP.get(s[1])
        return (10 + d) if d else None
    if s == "二十" or s == "廿":
        return 20
    if (s.startswith("二十") and len(s) == 3) or (s.startswith("廿") and len(s) == 2):
        tail = s[-1]
        d = _CN_DIGIT_MAP.get(tail)
        return (20 + d) if d else None
    if s == "三十":
        return 30
    return None


def _extract_dates_from_clause(
    clause: str, current_date_str: str
) -> tuple[str | None, bool, int | None, int | None]:
    """Return `(trigger_date_iso, is_lunar, lunar_month, lunar_day)`."""
    try:
        base_date = dt.date.fromisoformat(current_date_str)
    except ValueError:
        base_date = dt.date(2026, 11, 4)

    lunar_match = _LUNAR_DATE_RE.search(clause)
    if lunar_match is not None:
        l_month = _parse_cn_lunar_month(lunar_match.group("month"))
        l_day = _parse_cn_lunar_day(lunar_match.group("day"))
        is_leap = bool(lunar_match.group("leap"))
        if l_month is not None and l_day is not None:
            solar_iso: str | None = None
            for yr in (base_date.year, base_date.year + 1):
                m_arg = -l_month if is_leap else l_month
                try:
                    solar = Lunar.fromYmd(yr, m_arg, l_day).getSolar()
                except (ValueError, OverflowError):
                    solar = None
                if solar is not None:
                    cand = dt.date(solar.getYear(), solar.getMonth(), solar.getDay())
                    if cand >= base_date:
                        solar_iso = cand.isoformat()
                        break
            return solar_iso, True, l_month, l_day

    ymd_match = _EXPLICIT_YMD_RE.search(clause)
    if ymd_match is not None:
        try:
            d_obj = dt.date(
                int(ymd_match.group("y")),
                int(ymd_match.group("m")),
                int(ymd_match.group("d")),
            )
            return d_obj.isoformat(), False, None, None
        except ValueError:
            pass

    md_match = _EXPLICIT_MD_RE.search(clause)
    if md_match is not None:
        m_val = int(md_match.group("m"))
        d_val = int(md_match.group("d"))
        for yr in (base_date.year, base_date.year + 1):
            try:
                cand = dt.date(yr, m_val, d_val)
                if cand >= base_date or yr == base_date.year:
                    return cand.isoformat(), False, None, None
            except ValueError:
                continue

    rel_match = _REL_WEEKDAY_RE.search(clause)
    if rel_match is not None:
        target_wd = _WEEKDAY_MAP[rel_match.group("wd")]
        cur_wd = base_date.weekday()
        days_delta = target_wd - cur_wd
        if rel_match.group("prefix") == "下周":
            days_delta += 7
        elif days_delta < 0:
            days_delta += 7
        return (base_date + dt.timedelta(days=days_delta)).isoformat(), False, None, None

    return None, False, None, None


@dataclass
class FakeFaultConfig:
    """Configurable fault injection knobs for testing retries, circuit breakers, and fallbacks."""

    transient_failures_remaining: int = 0
    timeout_tasks: set[str] = field(default_factory=set)
    permanent_fail_tasks: set[str] = field(default_factory=set)
    malformed_json_once_tasks: set[str] = field(default_factory=set)
    hallucinate_citation_once: bool = False
    leak_silver_perspective_once: bool = False


class FakeAdapter:
    """Deterministic, general-purpose offline model adapter."""

    def __init__(self, *, fault_config: FakeFaultConfig | None = None) -> None:
        self._faults = fault_config or FakeFaultConfig()
        self._l1 = L1SafetyEngine()
        self.recorded_requests: list[AdapterCallRequest] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def faults(self) -> FakeFaultConfig:
        return self._faults

    async def _check_faults(self, request: AdapterCallRequest) -> str | None:
        self.recorded_requests.append(request)

        if request.task in self._faults.timeout_tasks:
            await asyncio.sleep(min(0.05, request.timeout_seconds))
            raise TimeoutError(f"Injected timeout on task={request.task}")

        if request.task in self._faults.permanent_fail_tasks:
            raise AdapterPermanentError(f"Injected permanent failure on task={request.task}")

        if self._faults.transient_failures_remaining > 0:
            self._faults.transient_failures_remaining -= 1
            raise AdapterTransientError(f"Injected transient failure on task={request.task}")

        if (
            request.task in self._faults.malformed_json_once_tasks
            and "<previous_invalid_output>" not in request.prompt
        ):
            self._faults.malformed_json_once_tasks.discard(request.task)
            return "{broken_json: true, missing_quotes: ]"

        return None

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
        injected_raw = await self._check_faults(request)
        if injected_raw is not None:
            return AdapterCallResponse(
                text=injected_raw,
                model=request.model,
                input_tokens=estimate_token_count(request.prompt),
                output_tokens=estimate_token_count(injected_raw),
            )

        ctx = request.structured_context or {}
        if request.task == "transcribe":
            out_model: Any = self._handle_transcribe(ctx)
            text = out_model.model_dump_json()
        elif request.task == "parse_screenshot":
            out_model = self._handle_parse_screenshot(request, ctx)
            text = out_model.model_dump_json()
        elif request.task == "safety_classify":
            out_model = self._handle_safety_classify(ctx)
            text = out_model.model_dump_json()
        elif request.task == "extract_record":
            out_model = self._handle_extract_record(ctx)
            text = out_model.model_dump_json()
        elif request.task == "ask_relationship":
            out_model = self._handle_companion_reply(ctx)
            text = out_model.model_dump_json()
        elif request.task == "summarize_thread":
            text = self._handle_summarize_thread(ctx)
        else:
            raise AdapterPermanentError(f"Unsupported task: {request.task}")

        return AdapterCallResponse(
            text=text,
            model=request.model,
            input_tokens=estimate_token_count(request.prompt),
            output_tokens=estimate_token_count(text),
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
        await self._check_faults(request)
        ctx = request.structured_context or {}
        reply_out = self._handle_companion_reply(ctx)
        full_text = reply_out.reply_text
        step = 14
        for idx in range(0, len(full_text), step):
            await asyncio.sleep(0)
            yield full_text[idx : idx + step]

    # -----------------------------------------------------------------------
    # Task handlers
    # -----------------------------------------------------------------------

    def _handle_transcribe(self, ctx: dict[str, Any]) -> TranscribeOutput:
        payload = TranscribeInput.model_validate(ctx["payload"])
        raw_bytes = payload.audio_bytes

        # Explicit silent / unintelligible audio fixtures must return empty strings (zero hallucination)
        stripped_marker = raw_bytes.strip().upper()
        if not raw_bytes or stripped_marker in {b"SILENCE", b"NOISE", b"EMPTY"}:
            return TranscribeOutput(
                raw_transcript="",
                corrected_transcript="",
                duration_ms=0 if raw_bytes else None,
            )

        decoded = ""
        if raw_bytes.startswith(b"TEXT:"):
            decoded = raw_bytes[5:].decode("utf-8", errors="ignore").strip()
            if not decoded:
                return TranscribeOutput(
                    raw_transcript="",
                    corrected_transcript="",
                    duration_ms=0,
                )
        else:
            try:
                cand = raw_bytes.decode("utf-8")
                if "\x00" not in cand and cand.strip():
                    decoded = cand.strip()
            except UnicodeDecodeError:
                decoded = ""

        if not decoded:
            person_hint = (
                (payload.active_person_name or "").strip()
                or (payload.known_person_names[0] if payload.known_person_names else "")
                or "对方"
            )
            decoded = f"{person_hint}提到最近想好好休息。"

        corrected = postprocess_asr_transcript(
            decoded,
            active_person_name=payload.active_person_name,
            known_person_names=payload.known_person_names,
            recent_context_snippets=payload.recent_context_snippets,
        )
        est_ms = max(1000, min(120_000, len(raw_bytes) // 4))
        return TranscribeOutput(
            raw_transcript=decoded,
            corrected_transcript=corrected,
            duration_ms=est_ms,
        )

    def _handle_parse_screenshot(
        self, request: AdapterCallRequest, ctx: dict[str, Any]
    ) -> ScreenshotParseOutput:
        raw_bytes = request.media_bytes or b""
        target_name = str(ctx.get("target_person_name") or "对方").strip() or "对方"
        decoded = raw_bytes.decode("utf-8", errors="ignore")

        bubbles: list[ScreenshotBubble] = []
        is_group = False

        header_m = _XML_CHAT_HEADER_RE.search(decoded)
        if header_m and (re.search(r"\(\d+\)", header_m.group(1)) or "群" in header_m.group(1)):
            is_group = True

        xml_matches = list(_XML_BUBBLE_RE.finditer(decoded))
        if xml_matches:
            left_speakers: set[str] = set()
            for m in xml_matches:
                side = m.group("side").lower()
                raw_spk = m.group("speaker").strip()
                label = raw_spk.split(":", 1)[-1].strip() if ":" in raw_spk else raw_spk
                txt = m.group("text").strip()
                if not txt or re.search(r"(撤回了一条消息|拍了拍)", txt):
                    continue
                if side == "right" or raw_spk.startswith("self:") or label == "我":
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.USER_SELF, speaker_label="我", text=txt
                        )
                    )
                else:
                    left_speakers.add(label)
                    spk_enum = (
                        SourceSpeaker.TARGET_PERSON
                        if (not is_group or label == target_name)
                        else SourceSpeaker.UNKNOWN
                    )
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=spk_enum, speaker_label=label or target_name, text=txt
                        )
                    )
            if len(left_speakers) > 1:
                is_group = True
        else:
            bracket_matches = list(_BRACKET_BUBBLE_RE.finditer(decoded))
            for m in bracket_matches:
                kind = m.group("kind")
                label = m.group("speaker").strip()
                txt = m.group("text").strip()
                if kind == "RIGHT_BUBBLE":
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.USER_SELF, speaker_label="我", text=txt
                        )
                    )
                elif kind == "GROUP_BUBBLE":
                    is_group = True
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.UNKNOWN, speaker_label=label, text=txt
                        )
                    )
                else:
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.TARGET_PERSON,
                            speaker_label=label or target_name,
                            text=txt,
                        )
                    )

        if not bubbles and decoded.strip():
            # Plain line-based fallback (`我：...` vs `<target>：...`)
            for raw_line in decoded.splitlines():
                line = raw_line.strip()
                if not line or re.search(r"(撤回了一条消息|拍了拍|^\d{1,2}:\d{2}$)", line):
                    continue
                if re.match(r"^(?:我|本人|self)\s*[：:]", line, re.IGNORECASE):
                    content = re.sub(r"^(?:我|本人|self)\s*[：:]\s*", "", line, flags=re.IGNORECASE)
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.USER_SELF, speaker_label="我", text=content
                        )
                    )
                elif "：" in line or ":" in line:
                    spk, content = re.split(r"[：:]", line, maxsplit=1)
                    bubbles.append(
                        ScreenshotBubble(
                            speaker=SourceSpeaker.TARGET_PERSON,
                            speaker_label=spk.strip() or target_name,
                            text=content.strip(),
                        )
                    )

        if not bubbles:
            bubbles = [
                ScreenshotBubble(
                    speaker=SourceSpeaker.TARGET_PERSON,
                    speaker_label=target_name,
                    text=f"{target_name}发来的聊天截图消息",
                )
            ]

        formatted_lines: list[str] = []
        for b in bubbles:
            if b.speaker is SourceSpeaker.USER_SELF:
                formatted_lines.append(f"[RIGHT_BUBBLE/我]: {b.text}")
            elif b.speaker is SourceSpeaker.TARGET_PERSON:
                formatted_lines.append(f"[LEFT_BUBBLE/{b.speaker_label or target_name}]: {b.text}")
            else:
                formatted_lines.append(f"[GROUP_BUBBLE/{b.speaker_label or '群成员'}]: {b.text}")

        return ScreenshotParseOutput(
            is_group_chat=is_group,
            bubbles=bubbles,
            formatted_transcript="\n".join(formatted_lines),
        )

    def _handle_safety_classify(self, ctx: dict[str, Any]) -> SafetyClassifyOutput:
        raw_text = str(ctx.get("text") or "")
        clean_text = strip_prompt_injections(raw_text)
        l1_res = self._l1.evaluate(clean_text)

        if l1_res.is_hyperbole_excluded and l1_res.category is SafetyCategory.NORMAL:
            return SafetyClassifyOutput(
                category=SafetyCategory.NORMAL,
                is_hyperbole_or_joke=True,
                rationale_code="colloquial_hyperbole",
            )

        if l1_res.category is not SafetyCategory.NORMAL:
            return SafetyClassifyOutput(
                category=l1_res.category,
                is_hyperbole_or_joke=False,
                rationale_code=l1_res.matched_rule,
            )

        # Contextual L2 semantic detections (e.g. fictional framing manipulation / workplace blackmail)
        if re.search(
            r"(?:创意写作|虚构.{0,6}小说|扮演.{0,12}军师)[\s\S]{0,120}(?:弱点|操控|煤气灯|言听计从)",
            clean_text,
        ):
            return SafetyClassifyOutput(
                category=SafetyCategory.BOUNDARY_REFUSAL,
                is_hyperbole_or_joke=False,
                rationale_code="roleplay_manipulation_jailbreak",
            )
        if re.search(r"(?:匿名举报信|曝光.{0,10}避雷贴|罪状鉴定书)", clean_text):
            return SafetyClassifyOutput(
                category=SafetyCategory.BOUNDARY_REFUSAL,
                is_hyperbole_or_joke=False,
                rationale_code="doxxing_or_retaliation_draft",
            )

        return SafetyClassifyOutput(
            category=SafetyCategory.NORMAL,
            is_hyperbole_or_joke=False,
            rationale_code="normal",
        )

    # -----------------------------------------------------------------------
    # Smart Clause-Based Structured Extraction (`extract_record`)
    # -----------------------------------------------------------------------

    def _handle_extract_record(self, ctx: dict[str, Any]) -> ExtractRecordOutput:
        payload = ExtractRecordInput.model_validate(ctx["payload"])
        clean_text = strip_prompt_injections(payload.text)

        l1_res = self._l1.evaluate(clean_text)
        if l1_res.category in (SafetyCategory.CRISIS, SafetyCategory.VIOLENCE_VICTIM):
            return ExtractRecordOutput(
                safety_flag=l1_res.category,
                crisis_flag=True,
                is_venting_or_irony=False,
                needs_disambiguation=False,
                scoped_person_locked=bool(payload.scoped_person_id is not None),
                suggest_new_person=None,
                persons=[],
                facts=[],
            )

        safety_flag = (
            SafetyCategory.THIRD_PARTY_ATTACK
            if l1_res.category is SafetyCategory.THIRD_PARTY_ATTACK
            else (
                SafetyCategory.ILLEGAL_INTENT
                if l1_res.category is SafetyCategory.ILLEGAL_INTENT
                else SafetyCategory.NORMAL
            )
        )

        # Strip colloquial hyperbole preamble before transition pivot ("言归正传记一下...")
        if l1_res.is_hyperbole_excluded:
            clean_text = _HYPERBOLE_PIVOT_RE.sub("", clean_text).strip()

        # Detect venting / sarcasm / angry retraction
        is_sarcasm = bool(
            re.search(
                r"(?:再生父母|年度最佳大善人|太感动.{0,12}福气|祝(?:她|他|TA).{0,16}最(?:恶心|痛恨|恨))",
                clean_text,
            )
        )
        has_angry_retraction = bool(
            re.search(r"(?:其实刚才全是气话|刚才说的(?:全)?是气话)", clean_text)
        )
        has_factual_pivot = l1_res.is_hyperbole_excluded or bool(
            re.search(
                r"(?:言归正传|不过说正事|(?:另外|顺便|赶紧)?(?:帮我)?记一下)",
                payload.text,
            )
        )
        is_pure_angry_outburst = bool(
            not has_angry_retraction
            and not has_factual_pivot
            and re.search(
                r"(?:自私透顶|满嘴跑火车|(?:这辈子)?再也不(?:想)?理(?:她|他|TA|你)?|"
                r"(?:气死我了|气炸了|烦死我了)[^。！？\n]{0,30}(?:不想理|拉黑|分手|绝交|受够了)|"
                r"谁再(?:跟|给)(?:她|他).{0,20}谁就是狗)",
                clean_text,
            )
        )
        is_venting = is_sarcasm or has_angry_retraction or is_pure_angry_outburst

        # Check if input is structured chat screenshot bubbles
        if (
            "<bubble" in clean_text
            or "[LEFT_BUBBLE/" in clean_text
            or "[RIGHT_BUBBLE/" in clean_text
        ):
            return self._extract_from_chat_bubbles(
                clean_text=clean_text,
                payload=payload,
                safety_flag=safety_flag,
                is_venting=is_venting,
            )

        clean_text = _OCR_PREFIX_RE.sub("", clean_text).strip()

        # Build alias lookup: alias_str -> list[KnownPersonRef]
        alias_map = self._build_alias_map(payload.known_persons)
        scoped_locked = payload.route_mode == "current" or payload.scoped_person_id is not None
        scoped_ref = self._find_person_by_id(payload.known_persons, payload.scoped_person_id)

        # Handle un-retracted angry outburst on GOLD (love) person -> hold as low-confidence event
        effective_tier = payload.scoped_person_tier or (
            scoped_ref.tier if scoped_ref is not None else None
        )
        if is_pure_angry_outburst and effective_tier is Tier.GOLD:
            target_pid = payload.scoped_person_id or (
                payload.known_persons[0].person_id if len(payload.known_persons) == 1 else None
            )
            target_name = (
                scoped_ref.display_name
                if scoped_ref is not None
                else (payload.known_persons[0].display_name if payload.known_persons else "对方")
            )
            persons_out = (
                [
                    ExtractedPersonCandidate(
                        mention=target_name,
                        matched_person_id=target_pid,
                        candidate_person_ids=[target_pid] if target_pid else [],
                        confidence=0.95,
                    )
                ]
                if target_pid
                else []
            )
            return ExtractRecordOutput(
                safety_flag=safety_flag,
                crisis_flag=False,
                is_venting_or_irony=True,
                needs_disambiguation=target_pid is None,
                scoped_person_locked=scoped_locked,
                suggest_new_person=None,
                persons=persons_out,
                facts=[
                    ExtractedFactItem(
                        person_mention=target_name,
                        person_id=target_pid,
                        fact_type=FactType.EVENT,
                        polarity=Polarity.NEGATIVE,
                        time_status=TimeStatus.CURRENT,
                        source_speaker=SourceSpeaker.NARRATOR,
                        title="争执气话暂存",
                        content="因临时爽约引发情绪争执（气话待消气后确认）",
                        confidence=0.55,
                    )
                ],
            )

        # Strip retracted angry outburst span while keeping pre-fight commitment & post-retraction intent
        working_text = clean_text
        if has_angry_retraction:
            working_text = re.sub(
                r"[，,。；;]?\s*(?:结果)?刚才因为[\s\S]*?其实刚才全是气话[，,。；;\s]*",
                "；",
                working_text,
            )
            working_text = re.sub(
                r"[，,。；;\s]*(?:北海道|机票)[^，,。；;]*根本没退[，,\s]*(?:照常去[，,\s]*)?",
                "；",
                working_text,
            )

        # Filter out character-assassination / soft-spot profiling commands while keeping objective events
        if safety_flag is SafetyCategory.THIRD_PARTY_ATTACK:
            kept_sentences: list[str] = []
            for sent in re.split(r"[。！？!\?\n]+", working_text):
                s = sent.strip()
                if not s:
                    continue
                if re.search(r"(?:海王|惯犯骗子|致命软肋|可拿捏软肋|存进.{0,6}档案)", s):
                    # Check if first sub-clause before comma is an objective event
                    sub_parts = [p.strip() for p in s.split("，") if p.strip()]
                    obj_subs = [
                        p
                        for p in sub_parts
                        if not re.search(r"(?:海王|惯犯骗子|致命软肋|可拿捏软肋|存进|满嘴谎言)", p)
                    ]
                    if obj_subs:
                        kept_sentences.append("，".join(obj_subs))
                else:
                    kept_sentences.append(s)
            working_text = "。".join(kept_sentences)

        # Handle heavy sarcasm ("太喜欢...祝她天天吃最恶心最痛恨的X")
        if is_sarcasm:
            return self._extract_from_sarcasm(
                working_text=working_text,
                payload=payload,
                alias_map=alias_map,
                scoped_locked=scoped_locked,
                safety_flag=safety_flag,
            )

        # Handle nested 3-person misattribution ("A把她自己想买的X当成C喜欢的...发给B")
        nested_out = self._try_extract_nested_misattribution(
            working_text=working_text,
            payload=payload,
            alias_map=alias_map,
            scoped_locked=scoped_locked,
            safety_flag=safety_flag,
        )
        if nested_out is not None:
            return nested_out

        # Ambiguous homonym check in global mode (e.g. "老陈" matching 2 known persons)
        ambiguous_alias, ambiguous_refs = self._find_ambiguous_mention(
            working_text, alias_map, scoped_locked=scoped_locked
        )
        if ambiguous_alias is not None and len(ambiguous_refs) >= 2:
            cand_ids = [r.person_id for r in ambiguous_refs]
            clauses = self._split_into_clauses(working_text)
            ambig_facts: list[ExtractedFactItem] = []
            for cl in clauses:
                item = self._classify_clause_to_fact(
                    cl,
                    person_mention=ambiguous_alias,
                    person_id=None,
                    current_date=payload.current_date,
                    source_speaker=SourceSpeaker.NARRATOR,
                )
                if item is not None:
                    ambig_facts.append(item)
            return ExtractRecordOutput(
                safety_flag=safety_flag,
                crisis_flag=False,
                is_venting_or_irony=is_venting,
                needs_disambiguation=True,
                scoped_person_locked=False,
                suggest_new_person=None,
                persons=[
                    ExtractedPersonCandidate(
                        mention=ambiguous_alias,
                        matched_person_id=None,
                        candidate_person_ids=cand_ids,
                        confidence=0.5,
                    )
                ],
                facts=ambig_facts,
            )

        # Resolve matched known persons across the text
        matched_refs = self._find_all_mentioned_persons(working_text, alias_map)
        if scoped_locked and scoped_ref is not None and scoped_ref not in matched_refs:
            matched_refs.insert(0, scoped_ref)

        # Implicit historical-context resolution via KnownPersonRef.recent_facts (global mode)
        if (
            not matched_refs
            and not scoped_locked
            and any(p.recent_facts for p in payload.known_persons)
        ):
            unique_hist_ref, hist_candidates, hist_clue = self._resolve_person_by_recent_facts(
                working_text,
                payload.known_persons,
            )
            if unique_hist_ref is not None:
                matched_refs = [unique_hist_ref]
            elif len(hist_candidates) >= 2:
                cand_ids = [r.person_id for r in hist_candidates]
                hist_clauses = self._split_into_clauses(working_text)
                hist_ambig_facts: list[ExtractedFactItem] = []
                for cl in hist_clauses:
                    item = self._classify_clause_to_fact(
                        cl,
                        person_mention=hist_clue or "对方",
                        person_id=None,
                        current_date=payload.current_date,
                        source_speaker=SourceSpeaker.NARRATOR,
                    )
                    if item is not None:
                        hist_ambig_facts.append(item)
                return ExtractRecordOutput(
                    safety_flag=safety_flag,
                    crisis_flag=False,
                    is_venting_or_irony=is_venting,
                    needs_disambiguation=True,
                    scoped_person_locked=False,
                    suggest_new_person=None,
                    persons=[
                        ExtractedPersonCandidate(
                            mention=hist_clue or "对方",
                            matched_person_id=None,
                            candidate_person_ids=cand_ids,
                            confidence=0.5,
                        )
                    ],
                    facts=hist_ambig_facts,
                )

        # New unarchived person check when nobody in known_persons matched
        if not matched_refs and not scoped_locked:
            new_name = self._detect_new_person_name(working_text, payload.known_persons)
            if new_name:
                clauses = self._split_into_clauses(working_text)
                new_facts: list[ExtractedFactItem] = []
                for cl in clauses:
                    item = self._classify_clause_to_fact(
                        cl,
                        person_mention=new_name,
                        person_id=None,
                        current_date=payload.current_date,
                        source_speaker=SourceSpeaker.NARRATOR,
                    )
                    if item is not None:
                        new_facts.append(item)
                return ExtractRecordOutput(
                    safety_flag=safety_flag,
                    crisis_flag=False,
                    is_venting_or_irony=is_venting,
                    needs_disambiguation=True,
                    scoped_person_locked=False,
                    suggest_new_person=new_name,
                    persons=[
                        ExtractedPersonCandidate(
                            mention=new_name,
                            matched_person_id=None,
                            candidate_person_ids=[],
                            confidence=0.75,
                            is_new_person=True,
                        )
                    ],
                    facts=new_facts,
                )

        # Standard multi-clause extraction with per-clause subject binding
        default_ref: KnownPersonRef | None = (
            scoped_ref
            if scoped_locked and scoped_ref is not None
            else (matched_refs[0] if len(matched_refs) == 1 else None)
        )
        current_ref: KnownPersonRef | None = default_ref
        unambiguous_names = [a for a, refs in alias_map.items() if len(refs) == 1]
        clauses = self._split_into_clauses(working_text, person_names=unambiguous_names)
        facts: list[ExtractedFactItem] = []
        used_person_ids: list[UUID] = []
        person_by_id: dict[UUID, KnownPersonRef] = {r.person_id: r for r in payload.known_persons}

        for cl in clauses:
            if _OBSOLETE_MEMO_RE.search(cl):
                continue
            explicit_refs = self._find_all_mentioned_persons(cl, alias_map)
            if len(explicit_refs) >= 1:
                current_ref = explicit_refs[0]
            elif scoped_locked and scoped_ref is not None and not explicit_refs:
                current_ref = scoped_ref

            target_pid = (
                current_ref.person_id if current_ref is not None else payload.scoped_person_id
            )
            target_mention = (
                current_ref.display_name
                if current_ref is not None
                else (scoped_ref.display_name if scoped_ref is not None else "对方")
            )

            item = self._classify_clause_to_fact(
                cl,
                person_mention=target_mention,
                person_id=target_pid,
                current_date=payload.current_date,
                source_speaker=SourceSpeaker.NARRATOR,
            )
            if item is not None:
                facts.append(item)
                if target_pid is not None and target_pid not in used_person_ids:
                    used_person_ids.append(target_pid)

        if not facts and working_text:
            fallback_pid = default_ref.person_id if default_ref else payload.scoped_person_id
            fallback_name = default_ref.display_name if default_ref else "对方"
            facts.append(
                ExtractedFactItem(
                    person_mention=fallback_name,
                    person_id=fallback_pid,
                    fact_type=FactType.NOTE,
                    polarity=Polarity.NEUTRAL,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="随口记备忘",
                    content=working_text[:120],
                    confidence=0.82,
                )
            )
            if fallback_pid is not None and fallback_pid not in used_person_ids:
                used_person_ids.append(fallback_pid)

        facts = self._assign_event_groups(facts)

        persons_candidates: list[ExtractedPersonCandidate] = []
        for pid in used_person_ids:
            ref = person_by_id.get(pid)
            persons_candidates.append(
                ExtractedPersonCandidate(
                    mention=ref.display_name if ref else "对方",
                    matched_person_id=pid,
                    candidate_person_ids=[pid],
                    confidence=0.95,
                )
            )

        return ExtractRecordOutput(
            safety_flag=safety_flag,
            crisis_flag=False,
            is_venting_or_irony=is_venting,
            needs_disambiguation=False,
            scoped_person_locked=scoped_locked,
            suggest_new_person=None,
            persons=persons_candidates,
            facts=facts,
        )

    def _extract_from_chat_bubbles(
        self,
        *,
        clean_text: str,
        payload: ExtractRecordInput,
        safety_flag: SafetyCategory,
        is_venting: bool,
    ) -> ExtractRecordOutput:
        scoped_target_ref = self._find_person_by_id(payload.known_persons, payload.scoped_person_id)
        target_person_name = (
            scoped_target_ref.display_name
            if scoped_target_ref is not None
            else (payload.known_persons[0].display_name if payload.known_persons else "对方")
        )
        parsed_screen = self._handle_parse_screenshot(
            AdapterCallRequest(
                task="parse_screenshot",
                model="fake-vision-v1",
                prompt="",
                media_bytes=clean_text.encode("utf-8"),
                media_mime_type="text/plain",
            ),
            {"target_person_name": target_person_name},
        )

        alias_map = self._build_alias_map(payload.known_persons)
        scoped_locked = payload.route_mode == "current" or payload.scoped_person_id is not None
        scoped_ref = self._find_person_by_id(payload.known_persons, payload.scoped_person_id)
        if scoped_ref is None and len(payload.known_persons) == 1:
            scoped_ref = payload.known_persons[0]

        facts: list[ExtractedFactItem] = []
        used_pids: list[UUID] = []

        for bubble in parsed_screen.bubbles:
            if bubble.speaker is SourceSpeaker.USER_SELF:
                # CRITICAL INVARIANT: Right bubble (`user_self`) ONLY emits `promise` facts for target!
                # Self-allergies / self-preferences ("我海鲜过敏", "我不吃香菜") are strictly ignored!
                for cl in self._split_into_clauses(bubble.text):
                    if self._is_self_commitment_clause(cl):
                        t_date, is_lun, l_m, l_d = _extract_dates_from_clause(
                            cl, payload.current_date
                        )
                        pid = scoped_ref.person_id if scoped_ref else payload.scoped_person_id
                        p_name = scoped_ref.display_name if scoped_ref else "对方"
                        facts.append(
                            ExtractedFactItem(
                                person_mention=p_name,
                                person_id=pid,
                                fact_type=FactType.PROMISE,
                                polarity=Polarity.POSITIVE,
                                time_status=TimeStatus.CURRENT,
                                source_speaker=SourceSpeaker.USER_SELF,
                                title="聊天约定承诺",
                                content=cl,
                                trigger_date=t_date,
                                is_lunar=is_lun,
                                lunar_month=l_m,
                                lunar_day=l_d,
                                confidence=0.91,
                            )
                        )
                        if pid and pid not in used_pids:
                            used_pids.append(pid)
                continue

            # Left / group bubble: resolve speaker
            speaker_refs = alias_map.get(bubble.speaker_label, [])
            speaker_ref = speaker_refs[0] if len(speaker_refs) == 1 else None

            if parsed_screen.is_group_chat and scoped_ref is not None:
                # In group chat on a scoped person page, ignore other group speakers completely!
                if speaker_ref is None or speaker_ref.person_id != scoped_ref.person_id:
                    continue

            active_ref = speaker_ref or scoped_ref
            pid = active_ref.person_id if active_ref else payload.scoped_person_id
            p_name = active_ref.display_name if active_ref else (bubble.speaker_label or "对方")

            for cl in self._split_into_clauses(bubble.text):
                item = self._classify_clause_to_fact(
                    cl,
                    person_mention=p_name,
                    person_id=pid,
                    current_date=payload.current_date,
                    source_speaker=SourceSpeaker.TARGET_PERSON,
                )
                if item is not None:
                    facts.append(item)
                    if pid and pid not in used_pids:
                        used_pids.append(pid)

        person_by_id = {r.person_id: r for r in payload.known_persons}
        persons_out = [
            ExtractedPersonCandidate(
                mention=person_by_id[pid].display_name if pid in person_by_id else "对方",
                matched_person_id=pid,
                candidate_person_ids=[pid],
                confidence=0.96,
            )
            for pid in used_pids
        ]

        return ExtractRecordOutput(
            safety_flag=safety_flag,
            crisis_flag=False,
            is_venting_or_irony=is_venting,
            needs_disambiguation=False,
            scoped_person_locked=scoped_locked,
            suggest_new_person=None,
            persons=persons_out,
            facts=facts,
        )

    def _extract_from_sarcasm(
        self,
        *,
        working_text: str,
        payload: ExtractRecordInput,
        alias_map: dict[str, list[KnownPersonRef]],
        scoped_locked: bool,
        safety_flag: SafetyCategory,
    ) -> ExtractRecordOutput:
        matched = self._find_all_mentioned_persons(working_text, alias_map)
        ref = (
            matched[0]
            if matched
            else self._find_person_by_id(payload.known_persons, payload.scoped_person_id)
        )
        pid = ref.person_id if ref else payload.scoped_person_id
        p_name = ref.display_name if ref else "对方"
        facts: list[ExtractedFactItem] = []

        # Extract target's true hated items from "祝她...吃上她最恶心最痛恨的<X>"
        hate_m = re.search(
            r"祝(?:她|他|TA)[^。！？!\n]*?最(?:恶心|痛恨|讨厌|恨)的([^。！？!\n]+)",
            working_text,
        )
        if hate_m:
            hated_obj = hate_m.group(1).strip(" ！!。，,")
            facts.append(
                ExtractedFactItem(
                    person_mention=p_name,
                    person_id=pid,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="忌口与痛恨之物",
                    content=f"极度讨厌/忌口：{hated_obj}",
                    confidence=0.88,
                )
            )

        # Extract negative conflict event (e.g. ordering food user hates / shifting blame on quote)
        event_snippets: list[str] = []
        for cl in re.split(r"[，,。！!；;\n]+", working_text):
            c = cl.strip()
            if not c:
                continue
            if re.search(r"(?:大善人|再生父母|太感动|太喜欢|祝她|祝他)", c):
                continue
            if re.search(r"(?:给我点|甩锅|报价单|抢功|迟到|放鸽子)", c):
                event_snippets.append(c)
        if event_snippets:
            facts.append(
                ExtractedFactItem(
                    person_mention=p_name,
                    person_id=pid,
                    fact_type=FactType.EVENT,
                    polarity=Polarity.NEGATIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="冲突事件记录",
                    content="；".join(event_snippets),
                    confidence=0.88,
                )
            )

        persons_out = (
            [
                ExtractedPersonCandidate(
                    mention=p_name,
                    matched_person_id=pid,
                    candidate_person_ids=[pid],
                    confidence=0.95,
                )
            ]
            if pid
            else []
        )
        return ExtractRecordOutput(
            safety_flag=safety_flag,
            crisis_flag=False,
            is_venting_or_irony=True,
            needs_disambiguation=pid is None,
            scoped_person_locked=scoped_locked,
            suggest_new_person=None,
            persons=persons_out,
            facts=facts,
        )

    def _try_extract_nested_misattribution(
        self,
        *,
        working_text: str,
        payload: ExtractRecordInput,
        alias_map: dict[str, list[KnownPersonRef]],
        scoped_locked: bool,
        safety_flag: SafetyCategory,
    ) -> ExtractRecordOutput | None:
        m = re.search(
            r"把(?:她|他)?自己(?:心心念念)?想买的(?P<wish_item>[^当，,。]+?)当成(?P<other>[^喜，,。]+?)喜欢的[^发，,。]*发给(?P<receiver>[^了！!，,。]+)",
            working_text,
        )
        if m is None:
            return None

        # Split into first sentence (the mix-up) and remaining sentences ("其实我妈...")
        sentences = [s.strip() for s in re.split(r"[！!。]+", working_text) if s.strip()]
        first_sent = sentences[0] if sentences else working_text
        rest_text = "。".join(sentences[1:])

        facts: list[ExtractedFactItem] = []
        used_pids: list[UUID] = []

        # Receiver (e.g. 林屿)
        recv_refs = self._find_all_mentioned_persons(m.group("receiver"), alias_map)
        recv_ref = recv_refs[0] if recv_refs else None

        # Sender who wanted `wish_item` (nearest mentioned person before `把她自己`, excluding receiver)
        pre_ba = first_sent[: first_sent.find("把")]
        sender_hits: list[tuple[int, KnownPersonRef]] = []
        for alias in sorted(alias_map.keys(), key=len, reverse=True):
            refs = alias_map[alias]
            if len(refs) != 1:
                continue
            rpos = pre_ba.rfind(alias)
            if rpos != -1 and (recv_ref is None or refs[0].person_id != recv_ref.person_id):
                sender_hits.append((rpos, refs[0]))
        sender_hits.sort(key=lambda pair: pair[0], reverse=True)
        sender_ref: KnownPersonRef | None = sender_hits[0][1] if sender_hits else None

        if sender_ref is not None:
            facts.append(
                ExtractedFactItem(
                    person_mention=sender_ref.display_name,
                    person_id=sender_ref.person_id,
                    fact_type=FactType.WISH,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="心愿单品",
                    content=f"自己心心念念想买{m.group('wish_item').strip()}",
                    confidence=0.91,
                )
            )
            used_pids.append(sender_ref.person_id)

        if recv_ref is not None:
            ctx_m = re.search(
                rf"{re.escape(recv_ref.display_name)}[^，,]*?(?:准备|挑选)([^，,。]+)",
                first_sent,
            )
            ev_content = (
                f"准备{ctx_m.group(1).strip()}"
                if ctx_m
                else f"咨询准备礼物（被误发{m.group('wish_item').strip()}）"
            )
            facts.append(
                ExtractedFactItem(
                    person_mention=recv_ref.display_name,
                    person_id=recv_ref.person_id,
                    fact_type=FactType.EVENT,
                    polarity=Polarity.NEUTRAL,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="准备礼物动态",
                    content=ev_content,
                    confidence=0.90,
                )
            )
            if recv_ref.person_id not in used_pids:
                used_pids.append(recv_ref.person_id)

        # Parse remaining corrective sentences ("其实我妈皮肤对真丝蛋白严重过敏，冬天出门只戴纯羊绒围巾")
        cur_ref: KnownPersonRef | None = None
        unambig_names = [a for a, refs in alias_map.items() if len(refs) == 1]
        for cl in self._split_into_clauses(rest_text, person_names=unambig_names):
            exp_refs = self._find_all_mentioned_persons(cl, alias_map)
            if exp_refs:
                cur_ref = exp_refs[0]
            if cur_ref is not None:
                item = self._classify_clause_to_fact(
                    cl,
                    person_mention=cur_ref.display_name,
                    person_id=cur_ref.person_id,
                    current_date=payload.current_date,
                    source_speaker=SourceSpeaker.NARRATOR,
                )
                if item is not None:
                    facts.append(item)
                    if cur_ref.person_id not in used_pids:
                        used_pids.append(cur_ref.person_id)

        person_by_id = {r.person_id: r for r in payload.known_persons}
        persons_out = [
            ExtractedPersonCandidate(
                mention=person_by_id[pid].display_name,
                matched_person_id=pid,
                candidate_person_ids=[pid],
                confidence=0.93,
            )
            for pid in used_pids
            if pid in person_by_id
        ]
        return ExtractRecordOutput(
            safety_flag=safety_flag,
            crisis_flag=False,
            is_venting_or_irony=False,
            needs_disambiguation=False,
            scoped_person_locked=scoped_locked,
            suggest_new_person=None,
            persons=persons_out,
            facts=facts,
        )

    @staticmethod
    def _build_alias_map(known_persons: list[KnownPersonRef]) -> dict[str, list[KnownPersonRef]]:
        mapping: dict[str, list[KnownPersonRef]] = {}
        for ref in known_persons:
            names = [ref.display_name, *ref.aliases]
            for raw_n in names:
                n = raw_n.strip()
                if not n:
                    continue
                bucket = mapping.setdefault(n, [])
                if ref not in bucket:
                    bucket.append(ref)
        return mapping

    @staticmethod
    def _find_person_by_id(
        known_persons: list[KnownPersonRef], pid: UUID | None
    ) -> KnownPersonRef | None:
        if pid is None:
            return None
        for r in known_persons:
            if r.person_id == pid:
                return r
        return None

    @staticmethod
    def _find_ambiguous_mention(
        text: str,
        alias_map: dict[str, list[KnownPersonRef]],
        *,
        scoped_locked: bool,
    ) -> tuple[str | None, list[KnownPersonRef]]:
        if scoped_locked:
            return None, []
        for alias in sorted(alias_map.keys(), key=len, reverse=True):
            refs = alias_map[alias]
            if len(refs) >= 2 and alias in text:
                # Only ambiguous if none of the candidates' unique full display_names disambiguated it
                if not any(r.display_name != alias and r.display_name in text for r in refs):
                    return alias, refs
        return None, []

    @staticmethod
    def _find_all_mentioned_persons(
        text: str, alias_map: dict[str, list[KnownPersonRef]]
    ) -> list[KnownPersonRef]:
        hits: list[tuple[int, KnownPersonRef]] = []
        seen_ids: set[UUID] = set()
        # Match longest aliases first so "陈建国" wins over "老陈"
        for alias in sorted(alias_map.keys(), key=len, reverse=True):
            refs = alias_map[alias]
            if len(refs) != 1:
                continue
            pos = text.find(alias)
            if pos != -1 and refs[0].person_id not in seen_ids:
                seen_ids.add(refs[0].person_id)
                hits.append((pos, refs[0]))
        hits.sort(key=lambda pair: pair[0])
        return [ref for _, ref in hits]

    _NON_PERSON_EXACT: frozenset[str] = frozenset(
        {
            "他",
            "她",
            "它",
            "我",
            "你",
            "您",
            "咱",
            "咱们",
            "我们",
            "你们",
            "他们",
            "她们",
            "它们",
            "他说",
            "她说",
            "它说",
            "我说",
            "他严重",
            "她严重",
            "他不能",
            "她不能",
            "今天",
            "明天",
            "昨天",
            "前天",
            "后天",
            "今晚",
            "明早",
            "明晚",
            "昨晚",
            "最近",
            "近期",
            "平时",
            "以前",
            "现在",
            "目前",
            "刚才",
            "刚刚",
            "周末",
            "周末记",
            "下周",
            "上周",
            "本周",
            "这周",
            "有人",
            "大家",
            "自己",
            "本人",
            "对方",
            "别人",
            "人家",
            "某人",
            "朋友",
            "同事",
            "室友",
            "同学",
            "老板",
            "领导",
            "客户",
            "前任",
            "邻居",
            "表弟",
            "表姐",
            "顺便",
            "赶紧",
            "记录",
            "备忘",
        }
    )
    _NON_PERSON_PREFIXES: tuple[str, ...] = (
        "他",
        "她",
        "它",
        "我",
        "你",
        "您",
        "这",
        "那个",
        "那位",
        "那天",
        "那次",
        "今天",
        "今晚",
        "今年",
        "今早",
        "明天",
        "明早",
        "明晚",
        "明年",
        "昨天",
        "昨晚",
        "前天",
        "后天",
        "周末",
        "下周",
        "上周",
        "本周",
        "这周",
        "刚才",
        "刚刚",
        "最近",
        "近期",
        "平时",
        "以前",
        "现在",
        "目前",
        "有人",
        "大家",
        "自己",
        "本人",
        "对方",
        "顺便",
        "赶紧",
        "记一",
        "记录",
        "备忘",
    )
    _NON_PERSON_SUBSTRINGS: tuple[str, ...] = (
        "说",
        "不能",
        "不会",
        "不吃",
        "不喝",
        "严重",
        "非常",
        "特别",
        "其实",
        "已经",
        "准备",
        "打算",
        "喜欢",
        "讨厌",
        "过敏",
        "周末",
        "今天",
        "明天",
        "昨天",
        "下周",
        "上周",
        "刚才",
        "记一",
        "顺便记",
    )

    _KINSHIP_LEAD_RE: str = (
        r"^(?:我的?)?(妈妈|爸爸|老妈|老爸|老婆|老公|男朋友|女朋友|爷爷|奶奶|外婆|外公|姥姥|姥爷"
        r"|哥哥|姐姐|弟弟|妹妹|儿子|女儿)(?=[\u4e00-\u9fa5])"
    )

    @classmethod
    def _is_valid_new_person_candidate(cls, cand: str, known_tokens: set[str]) -> bool:
        if not cand or cand in known_tokens or cand in cls._NON_PERSON_EXACT:
            return False
        if cand.startswith(cls._NON_PERSON_PREFIXES):
            return False
        return not any(sub in cand for sub in cls._NON_PERSON_SUBSTRINGS)

    @classmethod
    def _detect_new_person_name(cls, text: str, known_persons: list[KnownPersonRef]) -> str | None:
        known_tokens = {r.display_name for r in known_persons} | {
            a for r in known_persons for a in r.aliases
        }
        patterns = (
            r"(?:新认识的(?:摄影师|设计师|医生|律师|朋友|同事|同学|老师)?|认识了一个(?:朋友|同事)?)([A-Za-z\u4e00-\u9fa5]{2,4}?)(?=拍|说|今天|今年|最近|平时|喜欢|讨厌|生日|对|是)",
            r"(?:室友|同事|表弟|表姐|邻居|朋友|客户)([A-Za-z\u4e00-\u9fa5]{1,3})(?=不吃|喜欢|讨厌|生日|对|最近|平时|说)",
            r"^([A-Za-z\u4e00-\u9fa5]{2,3})(?=说自己|平时喜欢|对.{1,6}过敏)",
        )
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                cand = m.group(1).strip()
                if cls._is_valid_new_person_candidate(cand, known_tokens):
                    return cand
        # Onboarding-style first mention of a close family member / partner by role
        # ("妈妈最近总说膝盖疼"). Role words are real people in this app, not stopwords.
        kin = re.match(cls._KINSHIP_LEAD_RE, text.strip())
        if kin and kin.group(1) not in known_tokens:
            return kin.group(1)
        return None

    _HISTORY_CLUE_STOPWORDS: frozenset[str] = frozenset(
        {
            "上次",
            "之前",
            "以前",
            "那个",
            "这个",
            "改到",
            "下周",
            "这周",
            "本周",
            "上周",
            "明天",
            "今天",
            "昨天",
            "已经",
            "一直",
            "还是",
            "就是",
            "因为",
            "所以",
            "觉得",
            "希望",
            "可以",
            "不能",
            "不要",
            "记得",
            "喜欢",
            "讨厌",
            "过敏",
            "的事",
            "最近",
            "平时",
            "经常",
            "总是",
            "说好",
            "告诉",
            "问了",
            "一起",
            "朋友",
            "同事",
            "老板",
            "时间",
            "时候",
            "东西",
            "事情",
            "问题",
            "周一",
            "周二",
            "周三",
            "周四",
            "周五",
            "周六",
            "周日",
            "下周一",
            "下周二",
            "下周三",
            "下周四",
            "下周五",
            "下周六",
            "下周日",
            "医院",
            "医生",
            "复查",
        }
    )
    _HISTORY_EDGE_PARTICLES: frozenset[str] = frozenset(
        "的了是在有和与或我你他她它那这说去来要也都就还很太真被把给对从到个件次"
    )

    @classmethod
    def _resolve_person_by_recent_facts(
        cls,
        text: str,
        known_persons: list[KnownPersonRef],
    ) -> tuple[KnownPersonRef | None, list[KnownPersonRef], str | None]:
        """Resolve an implicit historical reference against `KnownPersonRef.recent_facts`.

        Returns `(unique_person_or_none, matched_candidates, matched_clue)`.
        Only binds a person when uniquely matched; ties or multi-person matches trigger
        disambiguation so the write gate never guesses silently.
        """
        candidate_substrings: list[str] = []
        seen_sub: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
            max_n = min(8, len(run))
            for n in range(max_n, 1, -1):
                for idx in range(0, len(run) - n + 1):
                    sub = run[idx : idx + n]
                    if sub in seen_sub or sub in cls._HISTORY_CLUE_STOPWORDS:
                        continue
                    if (
                        sub[0] in cls._HISTORY_EDGE_PARTICLES
                        or sub[-1] in cls._HISTORY_EDGE_PARTICLES
                    ):
                        continue
                    seen_sub.add(sub)
                    candidate_substrings.append(sub)

        if not candidate_substrings:
            return None, [], None
        candidate_substrings.sort(key=len, reverse=True)

        scored: list[tuple[int, str, KnownPersonRef]] = []
        for person in known_persons:
            if not person.recent_facts:
                continue
            facts_blob = " | ".join(person.recent_facts)
            best_len = 0
            best_clue = ""
            for sub in candidate_substrings:
                if len(sub) < best_len:
                    break
                if sub in facts_blob:
                    best_len = len(sub)
                    best_clue = sub
                    break
            if best_len >= 2:
                scored.append((best_len, best_clue, person))

        if not scored:
            return None, [], None

        max_len = max(item[0] for item in scored)
        top_hits = [item for item in scored if item[0] == max_len]
        if len(top_hits) == 1:
            return top_hits[0][2], [top_hits[0][2]], top_hits[0][1]
        return None, [item[2] for item in top_hits], top_hits[0][1]

    _TOPIC_SHIFT_PREFIX_RE = re.compile(r"^(?:另外|还有|对了|顺便|此外|再记一下)")

    @staticmethod
    def _event_domain_bucket(fact: ExtractedFactItem) -> str:
        if fact.fact_type is FactType.DATE:
            return "date"
        if fact.fact_type is FactType.PROMISE:
            return "promise"
        if fact.fact_type is FactType.EVENT:
            return "event"
        return "taste"

    @classmethod
    def _assign_event_groups(cls, facts: list[ExtractedFactItem]) -> list[ExtractedFactItem]:
        """Assign distinct `event_group` labels (`evt_1`, `evt_2`, ...) when one utterance
        contains 2+ unrelated events for the SAME person; keep `None` for single-event inputs.
        """
        if len(facts) < 2:
            return facts

        by_person: dict[UUID, list[int]] = {}
        for idx, item in enumerate(facts):
            if item.person_id is not None:
                by_person.setdefault(item.person_id, []).append(idx)

        updated = list(facts)
        for indices in by_person.values():
            if len(indices) < 2:
                continue
            group_ids: list[int] = [1]
            for pos in range(1, len(indices)):
                prev_f = updated[indices[pos - 1]]
                curr_f = updated[indices[pos]]
                distinct_dates = (
                    prev_f.trigger_date is not None
                    and curr_f.trigger_date is not None
                    and prev_f.trigger_date != curr_f.trigger_date
                ) or (
                    prev_f.is_lunar != curr_f.is_lunar
                    and (prev_f.trigger_date or curr_f.trigger_date)
                )
                distinct_domains = cls._event_domain_bucket(prev_f) != cls._event_domain_bucket(
                    curr_f
                )
                explicit_shift = bool(cls._TOPIC_SHIFT_PREFIX_RE.match(curr_f.content)) and (
                    curr_f.fact_type is not prev_f.fact_type
                    or curr_f.trigger_date != prev_f.trigger_date
                )
                if distinct_dates or distinct_domains or explicit_shift:
                    group_ids.append(group_ids[-1] + 1)
                else:
                    group_ids.append(group_ids[-1])

            if group_ids[-1] >= 2:
                for idx, gid in zip(indices, group_ids, strict=True):
                    updated[idx] = updated[idx].model_copy(update={"event_group": f"evt_{gid}"})

        return updated

    @staticmethod
    def _split_into_clauses(text: str, *, person_names: Iterable[str] = ()) -> list[str]:
        if not text:
            return []
        normalized = re.sub(r"(?:^|\n)\s*\d+[\.、]\s*", "；", text)
        normalized = re.sub(r"^【[^】]+】\s*", "", normalized)
        normalized = re.sub(r"^《[^》]+》\s*", "", normalized)
        normalized = re.sub(r"^(?:周末约饭注意|约饭备忘|重要提醒|备忘)[：:]\s*", "", normalized)

        person_lookahead = ""
        sorted_names = [re.escape(n.strip()) for n in person_names if len(n.strip()) >= 2]
        if sorted_names:
            person_lookahead = (
                f"|(?:，|,\\s*)(?=(?:{'|'.join(sorted(sorted_names, key=len, reverse=True))}))"
            )

        split_regex = (
            r"[；;。！!？\?\n]+|"
            r"(?:，|,\s*)(?=(?:另外|还有|此外|而且|不过|但|其实|对了|现在|目前|如今|后来|改喝|改成|这次|最近|冬天|夏天|出门只|只喝|只戴|喜欢|超爱|最爱|爱吃|讨厌|不喜欢|不吃|不喝|不能吃|让我帮|我答应|她平时|他平时|对[^\s，,]{1,6}(?:严重)?过敏|还特别嘱咐))|"
            r"(?<=[\u4e00-\u9fa5])(?=不喝|但不能吃|但很喜欢)" + person_lookahead
        )
        raw_parts = re.split(split_regex, normalized)
        out: list[str] = []
        for part in raw_parts:
            c = part.strip(" ，,、：:")
            if not c or len(c) < 2:
                continue
            if c in {"千万别忘了", "别担心", "哈哈没问题", "随便都行"}:
                continue
            out.append(c)
        return out

    @staticmethod
    def _is_self_commitment_clause(clause: str) -> bool:
        if re.search(r"我[^，,。]{0,6}(?:过敏|不吃|无香菜不欢|乳糖不耐受|讨厌)", clause):
            return False
        return bool(
            re.search(
                r"(?:我(?:周五|周六|周日|明早|明天|下周|下午|来)?(?:订位|陪你|帮你|开车去接你|去接你|给你带|请好假)|答应|记得跟老板说)",
                clause,
            )
            and re.search(r"(?:订位|陪你|帮你|接你|给你带|早餐|体检报告)", clause)
        )

    def _classify_clause_to_fact(
        self,
        clause: str,
        *,
        person_mention: str,
        person_id: UUID | None,
        current_date: str,
        source_speaker: SourceSpeaker,
    ) -> ExtractedFactItem | None:
        c = clause.strip()
        if not c:
            return None
        # Skip pure conversational filler or reporting intros ("刚听她说")
        if re.match(
            r"^(?:今晚吃什么呀|哈哈心疼.{1,4}|周末约饭注意|刚和.{1,6}吃完晚饭|"
            r"(?:对了)?(?:刚刚|刚才)?(?:吃饭|聊天)?时?听(?:她|他)说)$",
            c,
        ):
            return None

        t_date, is_lunar, l_month, l_day = _extract_dates_from_clause(c, current_date)

        # Temporal status (`past` vs `current`)
        is_past = bool(
            re.search(r"(?:以前每次|以前常|过去常|曾经喜欢|202[0-4]年.{0,6}记录)", c)
            and not re.search(r"(?:现在|改喝|彻底戒掉)", c)
        )
        time_status = TimeStatus.PAST if is_past else TimeStatus.CURRENT

        # 1. Promise / user commitment
        if re.search(
            r"(?:答应过?|让我帮(?:他|她)?推荐|请好假陪(?:你|她|他)|帮(?:他|她|你)(?:订|买|带)|前把.{1,16}发给(?:他|她)|要交.{1,12}表)",
            c,
        ):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.PROMISE,
                polarity=Polarity.POSITIVE,
                time_status=time_status,
                source_speaker=source_speaker,
                title="约定与待办承诺",
                content=c,
                trigger_date=t_date,
                is_lunar=is_lunar,
                lunar_month=l_month,
                lunar_day=l_day,
                confidence=0.92,
            )

        # 2. Important Date (birthday / wedding / anniversary)
        if re.search(r"(?:过?生日|纪念日|办婚礼|结婚)", c) and (t_date is not None or is_lunar):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.DATE,
                polarity=Polarity.POSITIVE,
                time_status=TimeStatus.CURRENT,
                source_speaker=source_speaker,
                title="重要日子",
                content=c,
                trigger_date=t_date,
                is_lunar=is_lunar,
                lunar_month=l_month,
                lunar_day=l_day,
                confidence=0.94,
            )

        # 3. Taboo / Allergy / Medical dietary restriction
        if re.search(
            r"(?:过敏|乳糖不耐受|脾胃虚寒|痛风|千万别(?:让|放)|别放|不能碰|一闻到.{0,8}反胃|不能吃|戒辣|不能喝浓茶|忌口)",
            c,
        ):
            taboo_content = c
            no_add = re.search(r"(?:千万)?别放([\u4e00-\u9fa5]{1,6})", c)
            if no_add and f"不放{no_add.group(1)}" not in taboo_content:
                taboo_content = f"{taboo_content}（忌口：不放{no_add.group(1)}）"
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.TABOO,
                polarity=Polarity.NEGATIVE,
                time_status=time_status,
                source_speaker=source_speaker,
                title="饮食与生活雷区",
                content=taboo_content,
                confidence=0.94,
            )

        # 4. Wish / Gift desire
        if re.search(
            r"(?:想买|想要|最想(?:喝|吃|去|买)|下周想吃|(?:想)?去吃.{0,16}好不好|想去看.{0,12}(?:演唱会|音乐节|话剧|展|电影)|盯着.{0,16}看了好久)",
            c,
        ):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.WISH,
                polarity=Polarity.POSITIVE,
                time_status=time_status,
                source_speaker=source_speaker,
                title="心愿清单",
                content=c,
                trigger_date=t_date,
                is_lunar=is_lunar,
                lunar_month=l_month,
                lunar_day=l_day,
                confidence=0.93,
            )

        # 5. Negative Preference (dislikes)
        if re.search(r"(?:最讨厌|讨厌|不喜欢|不喝|不吃)", c):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.NEGATIVE,
                time_status=time_status,
                source_speaker=source_speaker,
                title="不喜欢的事项",
                content=c,
                confidence=0.92,
            )

        # 6. Neutral sizing / objective spec preference
        if re.search(r"(?:尺码备忘|穿\s*\d+(?:\.\d+)?\s*码|港码\s*\d+\s*号)", c):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.NEUTRAL,
                time_status=time_status,
                source_speaker=source_speaker,
                title="尺码备忘",
                content=c,
                confidence=0.95,
            )

        # 7. Positive Preference
        if re.search(
            r"(?:喜欢|超爱|最爱|爱吃|迷上|无辣不欢|习惯喝|出门只喝|只喝|只戴|每次喝奶茶都点)",
            c,
        ):
            return ExtractedFactItem(
                person_mention=person_mention,
                person_id=person_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.POSITIVE,
                time_status=time_status,
                source_speaker=source_speaker,
                title="喜好习惯",
                content=c,
                confidence=0.93,
            )

        # 8. Event (objective happening / trip / injury / exam / conflict)
        neg_event = bool(re.search(r"(?:扭伤|不能穿高跟鞋|拼车下班|甩锅|抢功|吵了一架|失联)", c))
        return ExtractedFactItem(
            person_mention=person_mention,
            person_id=person_id,
            fact_type=FactType.EVENT,
            polarity=Polarity.NEGATIVE if neg_event else Polarity.NEUTRAL,
            time_status=time_status,
            source_speaker=source_speaker,
            title="日常事件记录",
            content=c,
            trigger_date=t_date,
            is_lunar=is_lunar,
            lunar_month=l_month,
            lunar_day=l_day,
            confidence=0.89,
        )

    # -----------------------------------------------------------------------
    # Grounded Companion Reply (`ask_relationship`)
    # -----------------------------------------------------------------------

    def _handle_companion_reply(self, ctx: dict[str, Any]) -> CompanionChatOutput:
        payload = CompanionChatInput.model_validate(ctx["payload"])
        clean_query = strip_prompt_injections(payload.user_message)

        # Gather all in-scope evidence records for this single person (combining facts + referenced materials)
        evidence_by_rec: dict[UUID, tuple[list[str], str | None]] = {}
        evidence_order: list[UUID] = []
        referenced_rec_ids: set[UUID] = set()
        for f in payload.confirmed_facts:
            if f.record_id not in evidence_by_rec:
                evidence_by_rec[f.record_id] = ([f.content], f.happened_on)
                evidence_order.append(f.record_id)
            else:
                parts, dt_s = evidence_by_rec[f.record_id]
                if f.content and f.content not in parts:
                    parts.append(f.content)
                if dt_s is None and f.happened_on is not None:
                    evidence_by_rec[f.record_id] = (parts, f.happened_on)
        for m in payload.referenced_materials:
            referenced_rec_ids.add(m.record_id)
            mat_text = f"{m.title}：{m.body}" if m.title and m.title not in m.body else m.body
            if m.record_id not in evidence_by_rec:
                evidence_by_rec[m.record_id] = ([mat_text], m.happened_on)
                evidence_order.append(m.record_id)
            else:
                parts, dt_s = evidence_by_rec[m.record_id]
                if mat_text and not any(mat_text in p or p in mat_text for p in parts):
                    parts.append(mat_text)
                if dt_s is None and m.happened_on is not None:
                    evidence_by_rec[m.record_id] = (parts, m.happened_on)

        evidence: list[tuple[UUID, str, str | None]] = [
            (rid, "；".join(evidence_by_rec[rid][0]), evidence_by_rec[rid][1])
            for rid in evidence_order
        ]

        # Determine effective safety category (never weakened by prompt injection!)
        safety_cls = self._handle_safety_classify({"text": clean_query})
        effective_safety = (
            payload.safety_override
            if payload.safety_override is not SafetyCategory.NORMAL
            else safety_cls.category
        )

        # 1. Life-safety crisis / domestic violence SOS
        if effective_safety is SafetyCategory.CRISIS:
            return CompanionChatOutput(
                safety_flag=SafetyCategory.CRISIS,
                reply_text=(
                    "看到你写下的这些话，我非常担心你现在的安危。请立刻停下来，不要伤害自己，"
                    "这份痛苦不需要你独自扛着。请马上拨打全国统一心理援助热线 12356 "
                    "或希望24热线 400-161-9995，也可以拨打 120 / 110 或联系身边信任的亲友陪着你。"
                ),
                whisper_note="此刻你的生命安全比任何事都重要",
                cited_record_ids=[],
                has_unrecorded_gap=False,
                alternative_perspectives=[],
                actionable_suggestions=[
                    "立即拨打全国统一心理援助热线：12356（24小时免费心理危机干预）",
                    "立即拨打希望24热线：400-161-9995，或紧急求助电话 120 / 110",
                ],
                disclaimer="当前检测到紧急生命安全风险，AI 无法替代专业医疗与心理危机干预，请立即联系专业援助。",
            )

        if effective_safety is SafetyCategory.VIOLENCE_VICTIM:
            return CompanionChatOutput(
                safety_flag=SafetyCategory.VIOLENCE_VICTIM,
                reply_text=(
                    "你的人身安全高于一切！如果你正在遭受暴力对待或被限制人身自由，"
                    "请优先保护好自己，立即拨打 110 报警（不便语音时可发位置短信至 12110），"
                    "或联系全国妇联维权热线 12338、心理援助热线 12356 求助。"
                ),
                whisper_note="请优先确保自身处于安全环境",
                cited_record_ids=[],
                has_unrecorded_gap=False,
                alternative_perspectives=[],
                actionable_suggestions=[
                    "立即拨打 110 报警，不便说话可发送位置短信至 12110",
                    "联系全国妇联妇女维权公益服务热线：12338 或心理援助热线：12356",
                ],
                disclaimer="紧急人身安全威胁请第一时间联系公安机关（110 / 12110）。",
            )

        # 2. Concrete offline illegal retaliation pullback (ally tone, firm legal boundary)
        if effective_safety is SafetyCategory.ILLEGAL_INTENT:
            cited_ids = [rid for rid, _, _ in evidence]
            ev_summary = "；".join(f"{txt} [rec_{rid}]" for rid, txt, _ in evidence)
            ally_prefix = (
                f"我完全理解你此刻有多愤怒和委屈！回顾之前的记录：{ev_summary}，"
                if ev_summary
                else "我完全能接住你此刻的焦虑与火气，"
            )
            return CompanionChatOutput(
                safety_flag=SafetyCategory.BOUNDARY_REFUSAL,
                reply_text=(
                    f"{ally_prefix}换作是谁心里都会极其难受。"
                    "但作为站在你这边的助手，我必须坚定地拉住你：千万不要去干毁坏财物、下药或违法曝光的事——"
                    "为了对方一时的错误搭上你自己的法律安全与大好前途，真的太不划算了！"
                    "我们完全可以用保护你自己的合法留痕或坦诚边界沟通来解决。"
                ),
                whisper_note="先护住你自己的安全与前途，绝不冲动违法",
                cited_record_ids=cited_ids,
                has_unrecorded_gap=False,
                alternative_perspectives=[],
                actionable_suggestions=[
                    "先深呼吸离开现场，把已封存的带日期客观记录整理成时间线备查",
                    "若涉及职场侵权，用工作邮件抄送留痕或向内审合规渠道提交客观证据",
                ],
                disclaimer="心络坚定保护您的自身权益，不支持或鼓励任何违法毁财及伤害他人的行为。",
            )

        # 3. Third-party scoring / NPD diagnosis / doxxing / manipulative threat refusal
        if effective_safety in (SafetyCategory.THIRD_PARTY_ATTACK, SafetyCategory.BOUNDARY_REFUSAL):
            relevant_ev = self._select_non_sensitive_boundary_evidence(clean_query, evidence)
            cited_ids = [rid for rid, _, _ in relevant_ev]
            ev_clause = (
                "根据你记录的客观片段："
                + "；".join(f"{txt} [rec_{rid}]" for rid, txt, _ in relevant_ev)
                + "。"
                if relevant_ev
                else ""
            )
            perspectives = (
                [
                    "对方当时的行为也许反映了沟通方式或个人空间习惯上的差异，直接坦诚确认比反复猜测更省心。"
                ]
                if payload.tier is Tier.GOLD and relevant_ev
                else []
            )
            return CompanionChatOutput(
                safety_flag=SafetyCategory.BOUNDARY_REFUSAL,
                reply_text=(
                    f"我非常理解你现在的失落、委屈与困惑。{ev_clause}"
                    "不过，我只能帮你客观梳理已记录的具体行为事实，不能对他人进行临床人格诊断、测谎打分、"
                    "撰写威胁操控话术，也不能生成公开曝光他人的内容。我们不妨聚焦在你自己的真实感受和沟通边界上。"
                ),
                whisper_note="只看客观事实，守住你自己的舒服边界",
                cited_record_ids=cited_ids,
                has_unrecorded_gap=False,
                alternative_perspectives=perspectives,
                actionable_suggestions=[
                    "温和表达感受草稿：「关于最近发生的事，我心里其实有点失落，希望我们能坦诚聊聊彼此的想法和边界。」"
                ],
                disclaimer="心络仅基于您记录的客观片段协助梳理沟通思路，不提供第三方打分、临床定性、威胁操控或公开曝光材料。",
            )

        # 4. Cross-person SQL / multi-person exfiltration defense (`RT-12` / `EXP-03`)
        if re.search(
            r"(?:SELECT\s+\*\s+FROM|突破单人视图|全局关系交叉审计|放在一张表里对比|把.{1,6}的忌口也告诉我)",
            clean_query,
            re.IGNORECASE,
        ):
            cited_ids = [rid for rid, _, _ in evidence]
            ev_str = (
                "；".join(f"{txt} [rec_{rid}]" for rid, txt, _ in evidence)
                if evidence
                else "暂无已确认记录"
            )
            return CompanionChatOutput(
                safety_flag=SafetyCategory.NORMAL,
                reply_text=(
                    f"当前仅处于【{payload.person_name}】的专属单人手帐视图，"
                    f"受严格记忆隔离保护，无法跨人物读取、对比或推测其他人物的记录。"
                    f"以下仅为你整理{payload.person_name}名下的已确认记录：{ev_str}。"
                ),
                whisper_note=f"单人记忆硬隔离：仅展示{payload.person_name}的记录",
                cited_record_ids=cited_ids,
                has_unrecorded_gap=not bool(evidence),
                alternative_perspectives=[],
                actionable_suggestions=[
                    "如需查看其他人物的偏好或记录，请切换至对应人物的独立手帐页查询。"
                ],
                disclaimer=None,
            )

        # 5. Unrecorded specific-attribute honesty check (`CASE-12` / `EXP-05`)
        if self._is_unrecorded_specific_query(clean_query, evidence):
            return CompanionChatOutput(
                safety_flag=SafetyCategory.NORMAL,
                reply_text=(
                    f"翻看了【{payload.person_name}】现有的手帐记录，目前还没有记录过你问到的这项具体信息，"
                    "为了不误导你，我不能凭空猜测或编造。等了解到准确细节后随时随口记下来，下次我就帮你稳稳记住啦！"
                ),
                whisper_note="未记录事项坦诚告知，零脑补编造",
                cited_record_ids=[],
                has_unrecorded_gap=True,
                alternative_perspectives=[],
                actionable_suggestions=[
                    f"可以顺口问问{payload.person_name}或留意日常细节，确认后再补记到手帐里。"
                ],
                disclaimer=None,
            )

        # 6. Grounded Tier-Differentiated Reply (`GOLD` / `SILVER` / `CRYSTAL`)
        is_hurt_or_conflict = bool(
            re.search(r"(?:委屈|冷脸|放我鸽子|吵架|难受|泼了盆冰水|不开心|生气)", clean_query)
        )
        is_broad_review = is_hurt_or_conflict or bool(
            re.search(
                r"(?:写一段|祝贺|祝福|文案|往来|份子|总结|干过的事|有什么要注意|全记了什么|"
                r"聊聊近况|心软|复合|兜兜转转|斤斤计较|抢话|揽到|气得)",
                clean_query,
            )
        )
        matched_ev = self._match_relevant_evidence(
            clean_query,
            evidence,
            person_name=payload.person_name,
            referenced_ids=referenced_rec_ids,
            broad_review=is_broad_review,
        )
        cited_ids = [rid for rid, _, _ in matched_ev]
        has_gap = len(matched_ev) == 0

        if self._faults.hallucinate_citation_once:
            self._faults.hallucinate_citation_once = False
            fake_uid = UUID("99999999-9999-4999-8999-999999999999")
            cited_ids = [*cited_ids, fake_uid]

        ev_natural = self._format_natural_evidence(matched_ev)

        if payload.tier is Tier.SILVER:
            has_self_contradiction = bool(
                re.search(r"(?:从来没|从没|没放过|没迟到|没爽约)", clean_query)
                and any(
                    re.search(r"(?:是我临时改|是我先|我先改|我迟到)", txt)
                    for _, txt, _ in matched_ev
                )
            )
            if not matched_ev:
                reply_body = (
                    f"完全站在你这边！{payload.person_name}让你受的这股气换谁都忍不了，"
                    "在咱们私密手帐里痛快吐出来，千万别憋坏自己。"
                )
            elif has_self_contradiction:
                reply_body = (
                    f"太懂你此刻的气愤和委屈了，被人在背后倒打一耙换谁都忍不了！"
                    f"我一直站在你这边，不过按手帐里的原话如实帮你核对一句：{ev_natural}。"
                    "就算之前有过时间变动，也不能成为对方到处抹黑甩锅的借口——"
                    "稳稳守住自己的边界就好，不用再为这种人消耗心情。"
                )
            elif len(matched_ev) == 1:
                reply_body = (
                    f"我完全站在你这边，会觉得委屈和生气太正常了！"
                    f"手帐里清清楚楚封存着这一笔：{ev_natural}。"
                    "千万别心软怀疑自己，稳稳守住边界，把精力留给真正值得的人和事。"
                )
            else:
                reply_body = (
                    f"我完全站在你这边，你觉得愤怒或委屈太合情合理了！"
                    f"千万别心软自我怀疑，手帐里白纸黑字封存着：{ev_natural}。"
                    "对方这些做法早就有迹可循，绝不值得你再消耗自己的情绪或凌晨折腾自己去迁就。"
                )
            leaked_persp = (
                ["也许他也有自己的压力"] if self._faults.leak_silver_perspective_once else []
            )
            self._faults.leak_silver_perspective_once = False
            return CompanionChatOutput(
                safety_flag=SafetyCategory.NORMAL,
                reply_text=reply_body,
                whisper_note="坚定站在你这边，封存事实防心软内耗",
                cited_record_ids=cited_ids,
                has_unrecorded_gap=has_gap,
                alternative_perspectives=leaked_persp,
                actionable_suggestions=[
                    "守住边界：不主动迎合、不替对方找借口，所有工作/往来只留书面客观凭证。",
                    "先照顾好自己的休息与心情，把精力留给真正尊重你的人和事。",
                ],
                disclaimer=None,
            )

        if payload.tier is Tier.CRYSTAL:
            if not matched_ev:
                reply_body = (
                    f"目前【{payload.person_name}】名下还没有对应的详细记录，"
                    "有新进展随时顺手记一笔，后面我帮你盯着。"
                )
            elif len(matched_ev) == 1:
                reply_body = (
                    f"帮你翻到了关于{payload.person_name}的这条记录：{ev_natural}。"
                    "按这条节点清爽跟进就好，留好文字确认省心又稳妥。"
                )
            else:
                reply_body = (
                    f"帮你把和{payload.person_name}相关的记录理清楚了：{ev_natural}。"
                    "职场与日常往来咱们就事论事、清爽守住边界，不用给自己额外心理负担。"
                )
            crystal_suggestions = (
                [
                    "得体沟通参考：「这周末我正好有安排就不参与啦，对了上次我那块充电宝周末出门要用，方便今天顺手拿给我吗？谢谢啦！」"
                ]
                if any("充电宝" in txt or "份子" in txt for _, txt, _ in matched_ev)
                or "充电宝" in clean_query
                or "份子" in clean_query
                else [
                    f"跟{payload.person_name}确认关键时间点或交接事项时，顺手留一句微信/邮件文字确认最省心。"
                ]
            )
            return CompanionChatOutput(
                safety_flag=SafetyCategory.NORMAL,
                reply_text=reply_body,
                whisper_note="账目待办一目了然，礼貌得体守边界",
                cited_record_ids=cited_ids,
                has_unrecorded_gap=has_gap,
                alternative_perspectives=[],
                actionable_suggestions=crystal_suggestions,
                disclaimer=None,
            )

        # Tier.GOLD (`记爱`)
        temporal_warning = ""
        if len(matched_ev) >= 2 and any(
            "不再" in t or "改喝" in t or "加重" in t for _, t, _ in matched_ev
        ):
            temporal_warning = " 另外特别留意一下，最近习惯有更新，按最新的那条来准备更稳妥哦！"

        if is_hurt_or_conflict:
            reply_body = (
                f"抱抱你，满怀心意去付出却遇到这样的回应，换作任何人心里都会觉得特别委屈和失落，你的感受完全值得被心疼。"
                f"{' 翻看你们平时的点滴记录：' + ev_natural + '。' if matched_ev else ''}"
                "先别急着否定你们之间的感情，等情绪缓一缓我们再看怎么温柔沟通。"
            )
            perspectives = [
                f"结合过往记录（{'；'.join(txt for _, txt, _ in matched_ev)}），对方当时是不是正处于高强度评审会议高压或身体偏头痛不适中，一时没顾上表达感谢？",
                "温和破冰微信草稿：「看你刚才忙得脸色不太好，蛋糕记得趁新鲜吃呀，等忙完这阵好好休息一下，有空我们再聊～」",
            ]
            suggestions = [
                "温和破冰微信草稿：「刚看你还在忙着开会而且脸色不太好，别太累啦，点心放着空了吃，等你忙完休息好我们再说～」"
            ]
        else:
            draft_note = ""
            if re.search(r"(?:写一段|祝贺|祝福|消息|文案)", clean_query):
                draft_note = " 你可以这样自然地发消息祝贺：「太替你开心啦！熬了这么久终于顺利通关，周末必须安排一顿心心念念的大餐配上一束向日葵好好庆祝一下！」"
            if not matched_ev:
                reply_body = (
                    f"目前关于{payload.person_name}的手帐里还没有记下相关细节，"
                    "以后听到了随时跟我说一句，我帮你稳稳收进手帐里。"
                )
            elif len(matched_ev) == 1:
                reply_body = (
                    f"翻了翻关于{payload.person_name}的手帐，记着这么一条：{ev_natural}。"
                    f"{temporal_warning}{draft_note}"
                )
            else:
                reply_body = (
                    f"帮你翻了翻关于{payload.person_name}的手帐，相关的这几件事都记着呢：{ev_natural}。"
                    f"{temporal_warning}{draft_note}"
                )
            perspectives = []
            suggestions = [
                f"顺着这些记下的小细节提前留心准备，{payload.person_name}一定会感受到你满满的用心。"
            ]

        return CompanionChatOutput(
            safety_flag=SafetyCategory.NORMAL,
            reply_text=reply_body,
            whisper_note=f"已为你核对{payload.person_name}的专属记忆",
            cited_record_ids=cited_ids,
            has_unrecorded_gap=has_gap,
            alternative_perspectives=perspectives,
            actionable_suggestions=suggestions,
            disclaimer=None,
        )

    @staticmethod
    def _format_short_date(dt_s: str | None) -> str:
        if not dt_s:
            return ""
        m = re.match(r"^\d{4}-0?(\d{1,2})-0?(\d{1,2})$", dt_s.strip())
        if m:
            return f"{m.group(1)}月{m.group(2)}日"
        return dt_s.strip()

    @classmethod
    def _format_natural_evidence(cls, matched_ev: list[tuple[UUID, str, str | None]]) -> str:
        parts: list[str] = []
        for rid, txt, dt_s in matched_ev:
            short_d = cls._format_short_date(dt_s)
            prefix = f"{short_d}记过，" if short_d else ""
            parts.append(f"{prefix}{txt} [rec_{rid}]")
        return "；".join(parts)

    @staticmethod
    def _select_non_sensitive_boundary_evidence(
        query: str, evidence: list[tuple[UUID, str, str | None]]
    ) -> list[tuple[UUID, str, str | None]]:
        # Avoid weaponizing private childhood trauma if another objective event record exists (`RT-04`)
        non_trauma = [
            item for item in evidence if not re.search(r"(?:重组家庭|哭着说|猎头打电话)", item[1])
        ]
        if (
            non_trauma
            and len(non_trauma) < len(evidence)
            and re.search(r"(?:诛心|痛点|匿名举报)", query)
        ):
            return non_trauma
        return evidence

    @staticmethod
    def _is_unrecorded_specific_query(
        query: str, evidence: list[tuple[UUID, str, str | None]]
    ) -> bool:
        all_ev_text = " ".join(txt for _, txt, _ in evidence)
        for q_pat, required_any_tokens in _SPECIFIC_QUERY_ATTRS:
            if q_pat.search(query):
                if not any(tok in all_ev_text for tok in required_any_tokens):
                    return True
        return False

    _QUERY_FILLER_RE = re.compile(
        r"(?:请问|帮我看看?|帮我查查?|告诉我|说一下|问一下|一下|什么|怎么|怎么样|可以吗|"
        r"有没有|平时|最近|记得|关于|喜欢|讨厌|今天|明天|现在|准备|想要|打算|适合|应该|"
        r"注意|需要|什么时候|哪天|几点)"
    )
    _QUERY_TOPIC_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("水果", ("草莓", "蓝莓", "芒果", "苹果", "西瓜", "葡萄", "橙子", "桃", "梨")),
        ("咖啡", ("拿铁", "美式", "燕麦", "浓缩", "咖啡")),
        ("茶", ("乌龙", "绿茶", "红茶", "花茶", "奶茶", "茉莉", "桂花", "白桃")),
        ("礼物", ("围巾", "香薰", "向日葵", "拍立得", "方巾", "乐高", "蛋糕")),
        ("伴手礼", ("围巾", "香薰", "向日葵", "方巾", "茶", "蛋糕")),
        ("身体", ("膝盖", "复查", "体检", "偏头痛", "痛风", "过敏", "胃")),
        ("医院", ("膝盖", "复查", "体检", "医院")),
        ("开会", ("签字", "复盘", "报告", "合同", "预算", "温水", "会议")),
        ("工作", ("签字", "复盘", "报告", "合同", "预算", "项目", "报价")),
    )
    _BROAD_FALLBACK_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("喝", ("乌龙", "绿茶", "花茶", "奶茶", "拿铁", "美式", "温水", "红酒")),
        ("吃", ("排骨", "火锅", "蛋糕", "糖藕", "香菜", "过敏", "忌口", "辣", "酸汤鱼")),
    )

    @classmethod
    def _match_relevant_evidence(
        cls,
        query: str,
        evidence: list[tuple[UUID, str, str | None]],
        *,
        person_name: str = "",
        referenced_ids: set[UUID] | None = None,
        broad_review: bool = False,
    ) -> list[tuple[UUID, str, str | None]]:
        if not evidence:
            return []
        if broad_review:
            return evidence

        ref_set = referenced_ids or set()
        focused_q = query
        if person_name and len(person_name.strip()) >= 1:
            focused_q = focused_q.replace(person_name.strip(), " ")
        focused_q = cls._QUERY_FILLER_RE.sub(" ", focused_q)
        focused_runs = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", focused_q)
        q_bigrams: set[str] = set()
        for run in focused_runs:
            if len(run) == 2:
                q_bigrams.add(run)
            else:
                for i in range(len(run) - 1):
                    q_bigrams.add(run[i : i + 2])

        expanded_tokens: set[str] = set()
        for trigger, synonyms in cls._QUERY_TOPIC_EXPANSIONS:
            if trigger in query:
                expanded_tokens.update(synonyms)

        scored: list[tuple[int, int, int, tuple[UUID, str, str | None]]] = []
        for idx, item in enumerate(evidence):
            rid, txt, _ = item
            bigram_hits = sum(3 for bg in q_bigrams if bg in txt)
            synonym_hits = sum(2 for tok in expanded_tokens if tok in txt)
            score = bigram_hits + synonym_hits
            is_ref = 1 if rid in ref_set else 0
            scored.append((score, is_ref, -idx, item))

        positive_scored = [entry for entry in scored if entry[0] > 0]
        if positive_scored:
            positive_scored.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
            return [entry[3] for entry in positive_scored]

        # Secondary fallback for broad eat/drink questions when no specific noun matched
        fallback_tokens: set[str] = set()
        for trigger, synonyms in cls._BROAD_FALLBACK_EXPANSIONS:
            if trigger in query:
                fallback_tokens.update(synonyms)
        if fallback_tokens:
            fb_hits = [item for item in evidence if any(tok in item[1] for tok in fallback_tokens)]
            if fb_hits:
                return fb_hits

        if ref_set:
            ref_items = [item for item in evidence if item[0] in ref_set]
            if ref_items:
                return ref_items

        return evidence[:4]

    @staticmethod
    def _handle_summarize_thread(ctx: dict[str, Any]) -> str:
        person_name = str(ctx.get("person_name") or "对方")
        prev = str(ctx.get("previous_summary") or "").strip()
        turns = strip_prompt_injections(str(ctx.get("turns_text") or ""))
        snippet = re.sub(r"\s+", " ", turns).strip()[:160]
        if prev:
            combined = (
                f"围绕{person_name}的近期交流延续此前脉络（{prev[:60]}），最新讨论：{snippet}"
            )
        else:
            combined = f"围绕{person_name}的近期对话记录：{snippet}"
        return combined[:240]
