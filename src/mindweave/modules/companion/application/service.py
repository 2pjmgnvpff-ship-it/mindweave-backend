"""Application use-case service for Cupid Companion Chat (REST & SSE)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.ai.schemas.contracts import CompanionChatOutput, SafetyDecision
from mindweave.core.clock import to_user_local
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import AppError, NotFoundError, UpstreamAIError, ValidationFailedError
from mindweave.core.idempotency import hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    BackgroundJobRow,
    ChatMessageRow,
    ChatThreadRow,
    FactRow,
    IdempotencyKeyRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    TelemetryEventRow,
    UserPreferenceRow,
)
from mindweave.modules.companion.application.dto import (
    ChatMessageDto,
    ChatMessagePage,
    ChatTurnResponse,
    ClearChatHistoryResponse,
    CrisisCardDto,
)
from mindweave.modules.companion.application.safety_fallback import BuiltinSafetyFallback
from mindweave.modules.companion.domain.citation_processor import (
    StreamingCitationProcessor,
    VerifiedCitation,
    process_complete_citations,
)
from mindweave.modules.companion.domain.output_guard import (
    FALLBACK_SAFE_REPLY,
    SILVER_LOYAL_FALLBACK_REPLY,
    SentenceBufferedStreamGuard,
    StreamingReplyExtractor,
    enforce_tier_output_constraints,
    sanitize_companion_output,
    sanitize_text,
)
from mindweave.modules.companion.domain.tier_policy import (
    ILLEGAL_INTENT_ALLY_PULLBACK,
    PUBLIC_SHAMING_BOUNDARY_GUIDANCE,
    SessionWindowDecision,
    append_late_night_whisper,
    apply_tier_policy,
    default_crisis_card_for,
    derive_stream_supplements,
    detect_unrecorded_gap,
    evaluate_session_window,
    is_public_shaming_request,
    should_writeback_chat_message,
)
from mindweave.modules.companion.infra.context_builder import (
    BuiltCompanionContext,
    CitationRecordMeta,
    ContextBuilder,
)
from mindweave.modules.companion.infra.repository import (
    CompanionRepository,
    HydratedCitation,
)
from mindweave.modules.memory.infra.search_cache import PersonSearchIndexCache
from mindweave.modules.reminders.domain.schedule import (
    ReminderScheduleError,
    resolve_next_lunar_occurrence_date,
    resolve_solar_yearly_date,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    ChatRole,
    FactStatus,
    FactType,
    PendingReason,
    RecordReviewState,
    ReminderStatus,
    SafetyCategory,
    Tier,
)
from mindweave.shared.pencil import format_fact_date_label
from mindweave.shared.scopes import PersonScope
from mindweave.workers.ports import (
    TASK_SUMMARIZE_THREAD,
    TASK_WRITEBACK_CHAT_FACTS,
    JobQueuePort,
)


def format_sse_event(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


_TAGGED_WHISPER_RE = re.compile(r"<whisper>(.*?)</whisper>", re.DOTALL | re.IGNORECASE)
_TAGGED_PERSPECTIVES_RE = re.compile(
    r"<perspectives>(.*?)</perspectives>", re.DOTALL | re.IGNORECASE
)
_TAGGED_SUGGESTIONS_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL | re.IGNORECASE)
_TAGGED_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL | re.IGNORECASE)

_PLANNING_ACTION_RE = re.compile(
    r"(?:提醒|清单|准备|复查|下周|明天|周末|生日|约好|截止|签字|日程|"
    r"挑.{0,4}礼物|买.{0,6}(?:围巾|礼物|蛋糕))"
)
_EXPLICIT_REMINDER_ASK_RE = re.compile(r"(?:提醒|设个?提醒|设一个提醒|提前提醒|闹钟|备忘提醒)")
_UPCOMING_CARE_CHECK_RE = re.compile(r"(?:留意|留心|最近|安排|准备|提醒|近况|注意|日程|要紧)")
_SILVER_DEADLINE_ACTION_RE = re.compile(
    r"(?:提醒|截止|签字|日程|明天|下周|周[一二三四五六日天]|复盘会)"
)
_SILVER_TIMELINE_ACTION_RE = re.compile(
    r"(?:时间线|整理|汇总|留痕|书面|邮件|起草|回复|记录|复盘|证据|经过|甩锅|改需求)"
)
_ISO_DATE_RE = re.compile(r"^(\d{4})-0?(\d{1,2})-0?(\d{1,2})$")
_LEADING_BRACKET_ISO_RE = re.compile(r"^\s*\[?\d{4}-\d{1,2}-\d{1,2}\]?\s*")
_EXPLICIT_CN_MD_RE = re.compile(
    r"(?<!\d)(?P<m>1[0-2]|0?[1-9])\s*月\s*(?P<d>[12]\d|3[01]|0?[1-9])\s*[日号]"
)
_REL_WEEKDAY_ACTION_RE = re.compile(r"(?P<prefix>这周|本周|下周)(?P<wd>[一二三四五六日天])")
_CN_WEEKDAY_IDX: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_PREP_REMINDER_MARKERS: tuple[str, ...] = ("准备", "清单", "提前")
_BRIEF_FACT_TYPE_ORDER: dict[str, int] = {
    FactType.TABOO.value: 0,
    FactType.DATE.value: 1,
    FactType.PROMISE.value: 2,
    FactType.WISH.value: 3,
    FactType.PREFERENCE.value: 4,
    FactType.EVENT.value: 5,
    FactType.NOTE.value: 6,
}


def _split_tag_items(raw_block: str) -> list[str]:
    lines = [
        line.strip().lstrip("-•*0123456789.、 ")
        for line in raw_block.strip().splitlines()
        if line.strip()
    ]
    return [item for item in lines if item]


def _clamp_clean_text(text: str, max_len: int, *, tier: Tier | None = None) -> str:
    cleaned = (
        enforce_tier_output_constraints(text, tier=tier, allow_empty=True)
        if tier is Tier.SILVER
        else sanitize_text(text, allow_empty=True).cleaned_text
    )
    compact = " ".join(cleaned.split()).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip("，,、；;：: ") + "…"


def _clip_at_sentence(text: str, max_len: int) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) <= max_len:
        return compact
    window = compact[:max_len]
    min_keep = max(12, int(max_len * 0.45))
    for seps in ("。！？", "；", "，,、"):
        pos = max((window.rfind(ch) for ch in seps), default=-1)
        if pos >= min_keep:
            return window[: pos + 1].rstrip("，,、；;：: ")
    return window[: max_len - 1].rstrip("，,、；;：: 0123456789") + "…"


def _format_silver_timeline_bullet(
    raw_text: str,
    happened_on: str | None,
    *,
    max_text_len: int = 72,
) -> str:
    cleaned = enforce_tier_output_constraints(raw_text, tier=Tier.SILVER, allow_empty=True)
    cleaned = _LEADING_BRACKET_ISO_RE.sub("", cleaned).strip()
    if not cleaned:
        return ""

    date_label = ""
    if happened_on:
        iso_m = _ISO_DATE_RE.match(happened_on.strip())
        if iso_m:
            month_num = int(iso_m.group(2))
            day_num = int(iso_m.group(3))
            date_label = f"{month_num}月{day_num}日"
            dup_date_re = re.compile(
                rf"^(?:\d{{4}}\s*年\s*)?0?{month_num}\s*月\s*0?{day_num}\s*[日号][\s，,：:、]*"
            )
            cleaned = dup_date_re.sub("", cleaned).strip()

    clipped = _clip_at_sentence(cleaned, max_text_len)
    if not clipped:
        return ""
    if date_label:
        return f"• {date_label}：{clipped}"
    return f"• {clipped}"


def _brief_date_prefix(when_label: str | None, title: str) -> str:
    if not when_label:
        return ""
    if when_label in title:
        return ""
    if when_label.endswith("日") and f"{when_label[:-1]}号" in title:
        return ""
    return when_label


def _format_cn_month_day(d: date) -> str:
    return f"{d.month}月{d.day}日"


def _extract_future_event_date(text: str, local_today: date) -> date | None:
    """Extract a concrete future Gregorian date mentioned in Chinese user text."""
    md_match = _EXPLICIT_CN_MD_RE.search(text)
    if md_match is not None:
        m_val = int(md_match.group("m"))
        d_val = int(md_match.group("d"))
        for yr in (local_today.year, local_today.year + 1):
            try:
                cand = date(yr, m_val, d_val)
            except ValueError:
                continue
            if cand > local_today:
                return cand

    rel_match = _REL_WEEKDAY_ACTION_RE.search(text)
    if rel_match is not None:
        target_wd = _CN_WEEKDAY_IDX[rel_match.group("wd")]
        delta = target_wd - local_today.weekday()
        if rel_match.group("prefix") == "下周":
            delta += 7
        elif delta <= 0:
            delta += 7
        return local_today + timedelta(days=delta)

    if "后天" in text:
        return local_today + timedelta(days=2)
    if "明天" in text:
        return local_today + timedelta(days=1)
    if "周末" in text:
        days_to_sat = (5 - local_today.weekday()) % 7
        if days_to_sat == 0:
            days_to_sat = 7
        return local_today + timedelta(days=days_to_sat)
    return None


def _compute_prep_trigger_date(event_date: date | None, local_today: date) -> date:
    """Return a future trigger date (>= tomorrow), preferring 1-2 days before an upcoming event."""
    tomorrow = local_today + timedelta(days=1)
    if event_date is None or event_date <= tomorrow:
        return tomorrow
    delta_days = (event_date - local_today).days
    if delta_days >= 3:
        return max(tomorrow, event_date - timedelta(days=1))
    return max(tomorrow, event_date - timedelta(days=1))


def _build_agent_step_events(ctx: BuiltCompanionContext) -> list[dict[str, Any]]:
    """Build calm, zero-telemetry `agent_step` SSE payloads grounded in `ContextBuilder`."""
    safe_name = sanitize_text(ctx.person_name, allow_empty=True).cleaned_text or "这位朋友"
    scanned = max(0, int(ctx.total_records_scanned))
    facts_cnt = max(0, int(ctx.confirmed_facts_count))
    rem_cnt = max(0, int(ctx.active_reminders_count))
    hit_cnt = max(0, int(ctx.recalled_records_count))

    scan_label = (
        f"正在梳理关于「{safe_name}」的时间线…"
        if ctx.tier is Tier.SILVER
        else f"正在翻看关于「{safe_name}」的手帐…"
    )
    steps: list[dict[str, Any]] = [
        {
            "step": 1,
            "phase": "scan",
            "label": scan_label,
            "count": scanned,
        }
    ]
    if facts_cnt > 0 or rem_cnt > 0:
        if facts_cnt > 0 and rem_cnt > 0:
            step2_label = "正在整理相关细节与日程…"
        elif facts_cnt > 0:
            step2_label = "正在整理相关细节…"
        else:
            step2_label = "正在查看近期日程…"
        steps.append(
            {
                "step": 2,
                "phase": "facts",
                "label": step2_label,
                "count": facts_cnt + rem_cnt,
            }
        )

    if hit_cnt > 0:
        steps.append(
            {
                "step": 3,
                "phase": "recall",
                "label": "已找到相关手帐记录",
                "count": hit_cnt,
                "detail": "、".join(ctx.recalled_dates[:3]),
            }
        )
    else:
        steps.append(
            {
                "step": 3,
                "phase": "recall",
                "label": "未找到更早的直接原话，将如实告知",
                "count": 0,
                "detail": "",
            }
        )
    return steps


def _derive_action_proposals(
    ctx: BuiltCompanionContext,
    *,
    user_message: str,
    assistant_reply: str,
    local_today: date,
    safety_category: SafetyCategory,
    has_unrecorded_gap: bool = False,
) -> list[dict[str, Any]]:
    """Deterministically propose up to 1-2 grounded 1-tap actions (NEVER on crisis/refusals)."""
    if safety_category is not SafetyCategory.NORMAL:
        return []

    explicit_reminder_ask = bool(_EXPLICIT_REMINDER_ASK_RE.search(user_message))
    if has_unrecorded_gap and not explicit_reminder_ask:
        return []

    proposals: list[dict[str, Any]] = []
    safe_name = sanitize_text(ctx.person_name, allow_empty=True).cleaned_text or "这位朋友"

    upcoming_soon = [
        r
        for r in ctx.upcoming_reminders
        if r.get("daysUntil") is not None
        and 0 <= int(r["daysUntil"]) <= 14
        and isinstance(r.get("triggerDate"), str)
    ]
    has_prep_reminder = any(
        any(marker in str(r.get("title") or "") for marker in _PREP_REMINDER_MARKERS)
        for r in upcoming_soon
    )

    msg_wants_reminder = bool(_PLANNING_ACTION_RE.search(user_message))
    user_asks_care_check = bool(_UPCOMING_CARE_CHECK_RE.search(user_message))
    silver_wants_timeline = ctx.tier is Tier.SILVER and bool(
        _SILVER_TIMELINE_ACTION_RE.search(user_message)
    )
    silver_wants_deadline = ctx.tier is Tier.SILVER and bool(
        _SILVER_DEADLINE_ACTION_RE.search(user_message)
    )

    should_propose_reminder = False
    if ctx.tier is Tier.SILVER:
        should_propose_reminder = silver_wants_deadline or (
            bool(upcoming_soon)
            and not has_prep_reminder
            and not silver_wants_timeline
            and user_asks_care_check
        )
    else:
        should_propose_reminder = msg_wants_reminder or (
            bool(upcoming_soon) and not has_prep_reminder and user_asks_care_check
        )

    if should_propose_reminder:
        explicit_date = _extract_future_event_date(user_message, local_today)
        matched_rem: dict[str, Any] | None = None
        if upcoming_soon:
            for rem in upcoming_soon:
                rem_title = str(rem.get("title") or "")
                if any(
                    kw in user_message and kw in rem_title
                    for kw in ("复查", "膝盖", "生日", "围巾", "看展", "海边", "签字", "合同")
                ):
                    matched_rem = rem
                    break
            if matched_rem is None and not msg_wants_reminder:
                matched_rem = upcoming_soon[0]

        event_date = explicit_date
        if event_date is None and matched_rem is not None:
            raw_td = str(matched_rem.get("triggerDate") or "")
            try:
                event_date = date.fromisoformat(raw_td[:10])
            except ValueError:
                event_date = None

        trigger_date = _compute_prep_trigger_date(event_date, local_today)

        # Ground reminder title strictly in recalled/confirmed context & user message (<= 36 chars)
        if matched_rem is not None and not explicit_date:
            rem_title_clean = str(matched_rem.get("title") or "").strip()
            raw_title = (
                f"准备{rem_title_clean}清单"
                if "复查" in rem_title_clean
                else f"提前准备：{rem_title_clean}"
            )
        elif "复查" in user_message:
            raw_title = f"陪{safe_name}复查前准备清单"
        elif "围巾" in user_message:
            raw_title = f"为{safe_name}准备围巾心意"
        elif "生日" in user_message:
            raw_title = f"提前准备{safe_name}的生日心意"
        elif "礼物" in user_message:
            raw_title = f"为{safe_name}准备心意礼物清单"
        elif any(k in user_message for k in ("签字", "合同", "截止")):
            raw_title = f"跟进{safe_name}事项截止节点"
        elif matched_rem is not None:
            raw_title = f"提前准备：{matched_rem['title']}"
        else:
            raw_title = f"关于{safe_name}的日程准备提醒"

        clean_title = _clamp_clean_text(raw_title, 36, tier=ctx.tier) or f"提醒：{safe_name}待办"
        proposals.append(
            {
                "id": str(new_id()),
                "kind": "create_reminder",
                "label": "提前设一个轻提醒",
                "title": clean_title,
                "triggerDate": trigger_date.isoformat(),
                "whenLabel": _format_cn_month_day(trigger_date),
            }
        )

    if silver_wants_timeline:
        # Calm, strictly objective timeline summary — NEVER push reconciliation!
        bullet_lines: list[str] = []
        seen_bullets: set[str] = set()
        for mat in ctx.chat_input.referenced_materials[:3]:
            line = _format_silver_timeline_bullet(mat.body, mat.happened_on)
            if line and line not in seen_bullets:
                seen_bullets.add(line)
                bullet_lines.append(line)
        if not bullet_lines:
            for fact in ctx.chat_input.confirmed_facts[:3]:
                line = _format_silver_timeline_bullet(fact.content, fact.happened_on)
                if line and line not in seen_bullets:
                    seen_bullets.add(line)
                    bullet_lines.append(line)
        if not bullet_lines:
            fallback_line = _format_silver_timeline_bullet(assistant_reply, None, max_text_len=140)
            if fallback_line:
                bullet_lines.append(fallback_line)

        note_body = "\n".join(bullet_lines[:3]).strip()
        if len(note_body) > 220:
            note_body = _clip_at_sentence(note_body, 220)
        if note_body:
            proposals.append(
                {
                    "id": str(new_id()),
                    "kind": "save_note",
                    "label": "把这份时间线要点存进手帐",
                    "title": _clamp_clean_text(f"{safe_name}关键时间线留痕", 36, tier=Tier.SILVER),
                    "body": note_body,
                }
            )

    return proposals[:2]


@dataclass(frozen=True, slots=True)
class PersonBriefItem:
    kind: Literal["reminder", "pencil", "fact"]
    when: str | None
    text: str
    record_id: UUID | None
    action_prompt: str


@dataclass(frozen=True, slots=True)
class PersonBriefResult:
    person_id: UUID
    tier: Tier
    upcoming_count: int
    pending_count: int
    record_count: int
    headline: str
    subline: str
    prompt: str
    items: list[PersonBriefItem]


@dataclass(slots=True)
class _PreparedChatTurn:
    replayed_turn: ChatTurnResponse | None
    idem_row_id: UUID | None
    context: BuiltCompanionContext | None
    user_msg_dto: ChatMessageDto | None
    thread_id: UUID | None
    prev_message_count: int
    session_decision: SessionWindowDecision | None
    safety_decision: SafetyDecision | None
    completed_crisis_turn: ChatTurnResponse | None = None


class CompanionService:
    """Orchestrates Cupid Companion Chat across REST & SSE while preserving all invariants."""

    def __init__(
        self,
        container: AppContainer,
        *,
        model_gateway: ModelGatewayPort | None = None,
        safety_service: SafetyServicePort | None = None,
        job_queue: JobQueuePort | None = None,
    ) -> None:
        self._container = container
        self._explicit_gateway = model_gateway
        self._explicit_safety = safety_service
        self._explicit_queue = job_queue
        self._repo = CompanionRepository(container.cipher)
        search_cache_raw = container.extras.get("search_index_cache")
        search_cache: PersonSearchIndexCache | None = (
            search_cache_raw if isinstance(search_cache_raw, PersonSearchIndexCache) else None
        )
        if search_cache is None:
            search_cache = PersonSearchIndexCache()
            container.extras["search_index_cache"] = search_cache
            container.cipher.register_eviction_hook(search_cache.evict_user)
        self._ctx_builder = ContextBuilder(
            container.cipher,
            default_timezone=container.settings.default_timezone,
            search_cache=search_cache,
        )

    def _resolve_gateway(self) -> ModelGatewayPort:
        gw: ModelGatewayPort | None = self._explicit_gateway
        if gw is None:
            for key in ("model_gateway", "gateway", "ai_gateway"):
                candidate = self._container.extras.get(key)
                if candidate is not None:
                    gw = candidate
                    break
        if gw is None:
            raise UpstreamAIError("AI Model Gateway is not configured")
        meter = getattr(gw, "meter", None)
        if meter is not None and hasattr(meter, "bind_db"):
            meter.bind_db(self._container.db)
        return gw

    @contextlib.asynccontextmanager
    async def _concurrency_slot(self, user_id: UUID) -> AsyncIterator[None]:
        limiter = self._container.extras.get("concurrency_limiter")
        if limiter is not None and hasattr(limiter, "slot"):
            async with limiter.slot(user_id):
                yield
            return
        yield

    def _resolve_safety(self) -> SafetyServicePort:
        if self._explicit_safety is not None:
            return self._explicit_safety
        for key in ("safety_service", "safety"):
            candidate = self._container.extras.get(key)
            if candidate is not None:
                return candidate  # type: ignore[no-any-return]
        return BuiltinSafetyFallback()

    def _resolve_job_queue(self) -> JobQueuePort | None:
        if self._explicit_queue is not None:
            return self._explicit_queue
        for key in ("job_queue", "queue"):
            candidate = self._container.extras.get(key)
            if candidate is not None:
                return candidate  # type: ignore[no-any-return]
        return None

    async def _enqueue_job(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        task_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        queue = self._resolve_job_queue()
        if queue is not None:
            await queue.enqueue(
                session,
                task_name=task_name,
                payload=payload,
                user_id=scope.user_id,
                idempotency_key=idempotency_key,
            )
            return

        now = self._container.clock.now()
        async with self._container.db.system_session() as sys_sess:
            stmt = (
                pg_insert(BackgroundJobRow)
                .values(
                    id=new_id(),
                    user_id=scope.user_id,
                    task_name=task_name,
                    payload_json=payload,
                    idempotency_key=idempotency_key,
                    status="queued",
                    attempts=0,
                    max_attempts=5,
                    run_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            await sys_sess.execute(stmt)

    async def list_messages(
        self,
        *,
        scope: PersonScope,
        cursor: str | None = None,
        limit: int = 20,
        order: Literal["asc", "desc"] = "asc",
    ) -> ChatMessagePage:
        async with self._container.db.user_session(scope.user_scope) as session:
            dek = await self._container.load_user_dek(session, scope.user_scope)
            hydrated, next_cursor, has_more = await self._repo.list_messages(
                session,
                scope=scope,
                dek=dek,
                cursor=cursor,
                limit=limit,
                order=order,
            )
        return ChatMessagePage(
            items=[ChatMessageDto.from_hydrated(m) for m in hydrated],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def clear_history(
        self,
        *,
        scope: PersonScope,
        idem_key: str | None = None,
        idem_path: str = "",
    ) -> ClearChatHistoryResponse:
        now = self._container.clock.now()
        async with self._container.db.user_session(scope.user_scope) as session:
            idem_row: IdempotencyKeyRow | None = None
            if idem_key is not None:
                effective_path = idem_path or f"/v1/persons/{scope.person_id}/chat/messages"
                req_hash = hash_request_payload(
                    {"personId": str(scope.person_id), "action": "clear_chat_history"}
                )
                idem_row, hit = await self._container.idempotency.claim_or_replay(
                    session,
                    scope=scope.user_scope,
                    idem_key=idem_key,
                    method="DELETE",
                    path=effective_path,
                    request_hash=req_hash,
                )
                if hit is not None:
                    return ClearChatHistoryResponse.model_validate(hit.response_body)

            deleted = await self._repo.clear_history(session, scope=scope, now=now)
            resp = ClearChatHistoryResponse(person_id=scope.person_id, deleted_count=deleted)
            if idem_row is not None:
                await self._container.idempotency.complete(
                    session,
                    idem_row,
                    status_code=200,
                    response_body=resp.model_dump(mode="json", by_alias=True),
                )
            return resp

    def _hydrate_citations(
        self,
        citations: Sequence[VerifiedCitation],
        catalog: dict[UUID, CitationRecordMeta],
    ) -> list[HydratedCitation]:
        hydrated: list[HydratedCitation] = []
        for cit in citations:
            meta = catalog.get(cit.record_id)
            if meta is None:
                continue
            hydrated.append(
                HydratedCitation(
                    index=cit.index,
                    material_id=cit.record_id,
                    title=meta.title,
                    snippet=meta.snippet,
                    happened_on=meta.happened_on,
                )
            )
        return hydrated

    async def _prepare_turn(
        self,
        *,
        scope: PersonScope,
        message: str,
        referenced_material_ids: Sequence[UUID],
        client_message_id: str | None,
        idem_key: str | None,
        idem_path: str,
    ) -> _PreparedChatTurn:
        cleaned_msg = message.strip()
        if not cleaned_msg:
            raise ValidationFailedError("Chat message must not be blank")

        safety_svc = self._resolve_safety()

        async with self._container.db.user_session(scope.user_scope) as session:
            # 1. Idempotency claim or replay (Invariant I11)
            idem_row: IdempotencyKeyRow | None = None
            if idem_key is not None:
                req_hash = hash_request_payload(
                    {
                        "personId": str(scope.person_id),
                        "message": cleaned_msg,
                        "referencedMaterialIds": [str(m) for m in referenced_material_ids],
                        "clientMessageId": client_message_id,
                    }
                )
                idem_row, hit = await self._container.idempotency.claim_or_replay(
                    session,
                    scope=scope.user_scope,
                    idem_key=idem_key,
                    method="POST",
                    path=idem_path,
                    request_hash=req_hash,
                )
                if hit is not None:
                    replayed = ChatTurnResponse.model_validate(hit.response_body)
                    return _PreparedChatTurn(
                        replayed_turn=replayed,
                        idem_row_id=None,
                        context=None,
                        user_msg_dto=None,
                        thread_id=None,
                        prev_message_count=0,
                        session_decision=None,
                        safety_decision=None,
                    )

            dek = await self._container.load_user_dek(session, scope.user_scope)

            norm_client_msg_id = (
                client_message_id.strip()
                if client_message_id and client_message_id.strip()
                else None
            )
            reused_user_row: ChatMessageRow | None = None
            if norm_client_msg_id is not None:
                latest_msg = await session.scalar(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.user_id == scope.user_id,
                        ChatMessageRow.person_id == scope.person_id,
                    )
                    .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
                    .limit(1)
                )
                if (
                    latest_msg is not None
                    and latest_msg.role == ChatRole.USER.value
                    and latest_msg.client_message_id == norm_client_msg_id
                ):
                    reused_user_row = latest_msg

            # 2. Build person-isolated context (verifies PersonRow & referenced_material_ids ownership!)
            now = self._container.clock.now()
            context = await self._ctx_builder.build(
                session,
                scope=scope,
                dek=dek,
                user_message=cleaned_msg,
                referenced_material_ids=referenced_material_ids,
                exclude_message_id=reused_user_row.id if reused_user_row is not None else None,
                now_utc=now,
            )

            # 3. Enforce daily chat quota BEFORE saving new user message (skip if reusing unanswered row)
            if reused_user_row is None:
                await self._container.quotas.check_chat_quota(
                    session,
                    scope.user_scope,
                    tz_name=context.timezone,
                )

            # 4. Update continuous session window & late-night care check on ChatThreadRow
            thread = await self._repo.get_or_create_thread_for_update(session, scope, now=now)
            prev_msg_count = (
                max(0, thread.message_count - 1)
                if reused_user_row is not None
                else thread.message_count
            )
            local_now = to_user_local(now, context.timezone)
            session_decision = evaluate_session_window(
                previous_session_started_at=thread.session_started_at,
                previous_last_message_at=thread.last_user_message_at,
                now_utc=now,
                local_hour=local_now.hour,
                tier=context.tier,
                late_night_care_enabled=context.late_night_care_enabled,
                late_night_start_hour=self._container.settings.late_night_start_hour,
                late_night_end_hour=self._container.settings.late_night_end_hour,
                late_night_continuous_minutes=self._container.settings.late_night_continuous_minutes,
            )
            thread.session_started_at = session_decision.session_started_at
            thread.last_user_message_at = session_decision.last_user_message_at
            thread.message_count = prev_msg_count + 1
            thread.updated_at = now

            # 5. Evaluate independent Safety channel FIRST (Invariant I5)
            safety_decision = await safety_svc.evaluate(
                scope=scope.user_scope,
                text=cleaned_msg,
                session=session,
            )

            # 6. Persist user ChatMessageRow encrypted with DEK + AAD (or reuse trailing unanswered row)
            if reused_user_row is not None:
                user_msg_row = reused_user_row
                user_msg_row.safety_category = safety_decision.category.value
                user_msg_row.referenced_record_ids = list(dict.fromkeys(referenced_material_ids))
                await session.flush()
            else:
                user_msg_row = await self._repo.insert_message(
                    session,
                    scope=scope,
                    dek=dek,
                    thread_id=thread.id,
                    role=ChatRole.USER,
                    content=cleaned_msg,
                    tier_snapshot=context.tier,
                    now=now,
                    referenced_record_ids=list(dict.fromkeys(referenced_material_ids)),
                    safety_category=safety_decision.category,
                    client_message_id=norm_client_msg_id,
                )
            user_hydrated = self._repo.decrypt_message_row(
                user_msg_row, scope=scope, dek=dek, citations=[]
            )
            user_msg_dto = ChatMessageDto.from_hydrated(user_hydrated)

            # 7. If crisis / violence_victim: persist fixed audited crisis response immediately (NO LLM call!)
            is_crisis = safety_decision.block_normal_reply or safety_decision.category in (
                SafetyCategory.CRISIS,
                SafetyCategory.VIOLENCE_VICTIM,
            )
            if is_crisis:
                card_payload = safety_decision.crisis_card or default_crisis_card_for(
                    safety_decision.category
                )
                asst_now = self._container.clock.now()
                assistant_row = await self._repo.insert_message(
                    session,
                    scope=scope,
                    dek=dek,
                    thread_id=thread.id,
                    role=ChatRole.ASSISTANT,
                    content=card_payload.care_message,
                    tier_snapshot=context.tier,
                    now=asst_now,
                    safety_category=safety_decision.category,
                    has_unrecorded_gap=False,
                    prompt_version="sop_crisis_v0.2",
                )
                thread.message_count = prev_msg_count + 2
                thread.updated_at = asst_now

                # Zero-PII safety telemetry (PRD §8 `crisis_flow_entered`)
                session.add(
                    TelemetryEventRow(
                        id=new_id(),
                        user_id=scope.user_id,
                        event_name="crisis_flow_entered",
                        metadata_json={
                            "triggerCategory": safety_decision.category.value,
                            "triggerSource": safety_decision.trigger_source,
                        },
                        occurred_at=asst_now,
                        created_at=asst_now,
                    )
                )
                await session.flush()

                asst_hydrated = self._repo.decrypt_message_row(
                    assistant_row, scope=scope, dek=dek, citations=[]
                )
                asst_dto = ChatMessageDto.from_hydrated(asst_hydrated)
                crisis_turn = ChatTurnResponse(
                    message_id=asst_dto.id,
                    user_message=user_msg_dto,
                    assistant_message=asst_dto,
                    tier_snapshot=context.tier,
                    has_unrecorded_gap=False,
                    cited_material_ids=[],
                    crisis_card=CrisisCardDto.from_payload(card_payload),
                )
                if idem_row is not None:
                    await self._container.idempotency.complete(
                        session,
                        idem_row,
                        status_code=200,
                        response_body=crisis_turn.to_idempotency_payload(),
                    )
                return _PreparedChatTurn(
                    replayed_turn=None,
                    idem_row_id=None,
                    context=context,
                    user_msg_dto=user_msg_dto,
                    thread_id=thread.id,
                    prev_message_count=prev_msg_count,
                    session_decision=session_decision,
                    safety_decision=safety_decision,
                    completed_crisis_turn=crisis_turn,
                )

            # Propagate safety_override (e.g. ILLEGAL_INTENT / THIRD_PARTY_ATTACK) into CompanionChatInput
            if safety_decision.category is not SafetyCategory.NORMAL:
                updated_input = context.chat_input.model_copy(
                    update={"safety_override": safety_decision.category}
                )
                context = replace(context, chat_input=updated_input)

            return _PreparedChatTurn(
                replayed_turn=None,
                idem_row_id=idem_row.id if idem_row is not None else None,
                context=context,
                user_msg_dto=user_msg_dto,
                thread_id=thread.id,
                prev_message_count=prev_msg_count,
                session_decision=session_decision,
                safety_decision=safety_decision,
            )

    async def _finalize_and_persist_assistant_turn(
        self,
        *,
        prepared: _PreparedChatTurn,
        raw_message: str,
        output: CompanionChatOutput,
        citations: Sequence[VerifiedCitation],
        prompt_version: str | None,
    ) -> ChatTurnResponse:
        assert prepared.context is not None
        assert prepared.user_msg_dto is not None
        assert prepared.thread_id is not None
        assert prepared.session_decision is not None
        assert prepared.safety_decision is not None

        ctx = prepared.context
        scope = ctx.scope

        # Apply deterministic 3-tier Cupid policy + anti-dependency/anti-jargon guardrails
        policed = apply_tier_policy(
            output,
            tier=ctx.tier,
            user_message=raw_message,
            safety_category=prepared.safety_decision.category,
        )
        sanitized, _report = sanitize_companion_output(policed, tier=ctx.tier)

        # Append SOP late-night rest care whisper if continuous session threshold met
        final_whisper = append_late_night_whisper(
            sanitized.whisper_note,
            prepared.session_decision.care_whisper,
        )
        if final_whisper:
            final_whisper = sanitize_text(final_whisper, allow_empty=True).cleaned_text or None

        final_perspectives = (
            list(sanitized.alternative_perspectives) if ctx.tier is Tier.GOLD else []
        )
        final_suggestions = list(sanitized.actionable_suggestions)

        # Keep only citations whose record_id exists in this turn's PersonScope catalog
        valid_citations = [c for c in citations if c.record_id in ctx.allowed_record_ids]
        hydrated_cits = self._hydrate_citations(valid_citations, ctx.record_catalog)

        async with self._container.db.user_session(scope.user_scope) as session:
            dek = await self._container.load_user_dek(session, scope.user_scope)
            now = self._container.clock.now()
            thread = await self._repo.get_or_create_thread_for_update(session, scope, now=now)

            assistant_row = await self._repo.insert_message(
                session,
                scope=scope,
                dek=dek,
                thread_id=thread.id,
                role=ChatRole.ASSISTANT,
                content=sanitized.reply_text,
                tier_snapshot=ctx.tier,
                now=now,
                whisper_note=final_whisper,
                perspectives=final_perspectives,
                suggestions=final_suggestions,
                referenced_record_ids=[c.record_id for c in valid_citations],
                safety_category=sanitized.safety_flag,
                has_unrecorded_gap=sanitized.has_unrecorded_gap,
                prompt_version=prompt_version,
            )
            await self._repo.insert_citations(
                session,
                scope=scope,
                message_id=assistant_row.id,
                citations=valid_citations,
                now=now,
            )

            prev_total = prepared.prev_message_count
            new_total = max(thread.message_count + 1, prev_total + 2)
            thread.message_count = new_total
            thread.updated_at = now

            # Zero-PII telemetry (`ask_replied` per PRD §8)
            session.add(
                TelemetryEventRow(
                    id=new_id(),
                    user_id=scope.user_id,
                    event_name="ask_replied",
                    metadata_json={
                        "citedCount": len(valid_citations),
                        "hasGap": bool(sanitized.has_unrecorded_gap),
                        "tier": ctx.tier.value,
                    },
                    occurred_at=now,
                    created_at=now,
                )
            )

            # Enqueue async fact write-back when user message contains declarative statements
            if should_writeback_chat_message(raw_message, sanitized.safety_flag):
                await self._enqueue_job(
                    session,
                    scope=scope,
                    task_name=TASK_WRITEBACK_CHAT_FACTS,
                    payload={
                        "user_id": str(scope.user_id),
                        "person_id": str(scope.person_id),
                        "message_id": str(prepared.user_msg_dto.id),
                    },
                    idempotency_key=f"chat-writeback:{prepared.user_msg_dto.id}",
                )

            # Enqueue rolling thread summarization every 12 messages
            if (prev_total // 12) < (new_total // 12):
                bucket = new_total // 12
                await self._enqueue_job(
                    session,
                    scope=scope,
                    task_name=TASK_SUMMARIZE_THREAD,
                    payload={
                        "user_id": str(scope.user_id),
                        "person_id": str(scope.person_id),
                        "thread_id": str(thread.id),
                        "bucket": bucket,
                    },
                    idempotency_key=f"chat-summarize:{thread.id}:{bucket}",
                )

            asst_hydrated = self._repo.decrypt_message_row(
                assistant_row,
                scope=scope,
                dek=dek,
                citations=hydrated_cits,
            )
            asst_dto = ChatMessageDto.from_hydrated(asst_hydrated)
            turn_resp = ChatTurnResponse(
                message_id=asst_dto.id,
                user_message=prepared.user_msg_dto,
                assistant_message=asst_dto,
                tier_snapshot=ctx.tier,
                has_unrecorded_gap=asst_dto.has_unrecorded_gap,
                cited_material_ids=asst_dto.cited_material_ids,
                crisis_card=None,
            )

            if prepared.idem_row_id is not None:
                idem_row = await session.get(IdempotencyKeyRow, prepared.idem_row_id)
                if idem_row is not None:
                    await self._container.idempotency.complete(
                        session,
                        idem_row,
                        status_code=200,
                        response_body=turn_resp.to_idempotency_payload(),
                    )

            return turn_resp

    async def _release_failed_idempotency(
        self,
        scope: PersonScope,
        idem_row_id: UUID | None,
        *,
        idem_key: str | None = None,
        idem_path: str = "",
        user_message_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> None:
        if idem_key is None and idem_row_id is None and user_message_id is None:
            return

        async def _do_release() -> None:
            async with self._container.db.user_session(scope.user_scope) as sess:
                if idem_key is not None and idem_path:
                    await self._container.idempotency.release(
                        sess,
                        scope=scope.user_scope,
                        idem_key=idem_key,
                        method="POST",
                        path=idem_path,
                    )
                elif idem_row_id is not None:
                    await sess.execute(
                        delete(IdempotencyKeyRow).where(
                            IdempotencyKeyRow.user_id == scope.user_id,
                            IdempotencyKeyRow.id == idem_row_id,
                            IdempotencyKeyRow.status == "in_progress",
                        )
                    )

                if user_message_id is not None:
                    thread_row: ChatThreadRow | None = None
                    if thread_id is not None:
                        thread_row = await sess.scalar(
                            select(ChatThreadRow)
                            .where(
                                ChatThreadRow.user_id == scope.user_id,
                                ChatThreadRow.id == thread_id,
                            )
                            .with_for_update()
                        )
                    orphan_user_msg = await sess.scalar(
                        select(ChatMessageRow).where(
                            ChatMessageRow.user_id == scope.user_id,
                            ChatMessageRow.id == user_message_id,
                            ChatMessageRow.role == ChatRole.USER.value,
                        )
                    )
                    if orphan_user_msg is not None:
                        has_assistant_after = await sess.scalar(
                            select(ChatMessageRow.id)
                            .where(
                                ChatMessageRow.user_id == scope.user_id,
                                ChatMessageRow.thread_id == orphan_user_msg.thread_id,
                                ChatMessageRow.role == ChatRole.ASSISTANT.value,
                                tuple_(ChatMessageRow.created_at, ChatMessageRow.id)
                                > (orphan_user_msg.created_at, orphan_user_msg.id),
                            )
                            .limit(1)
                        )
                        if has_assistant_after is None:
                            await sess.delete(orphan_user_msg)
                            if thread_row is not None:
                                thread_row.message_count = max(0, thread_row.message_count - 1)
                            await sess.flush()

        cleanup_task = asyncio.create_task(_do_release())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await cleanup_task
            raise

    async def send_message_json(
        self,
        *,
        scope: PersonScope,
        message: str,
        referenced_material_ids: Sequence[UUID] = (),
        client_message_id: str | None = None,
        idem_key: str,
        idem_path: str,
    ) -> ChatTurnResponse:
        prepared = await self._prepare_turn(
            scope=scope,
            message=message,
            referenced_material_ids=referenced_material_ids,
            client_message_id=client_message_id,
            idem_key=idem_key,
            idem_path=idem_path,
        )
        if prepared.replayed_turn is not None:
            return prepared.replayed_turn
        if prepared.completed_crisis_turn is not None:
            return prepared.completed_crisis_turn

        assert prepared.context is not None
        assert prepared.safety_decision is not None
        ctx = prepared.context

        # Short-circuit public shaming/doxxing card requests without risking unsafe generation
        if is_public_shaming_request(message):
            shaming_out = CompanionChatOutput(
                safety_flag=SafetyCategory.BOUNDARY_REFUSAL,
                reply_text=PUBLIC_SHAMING_BOUNDARY_GUIDANCE,
                whisper_note="记录仅自己可见，重要沟通建议保留客观书面留痕",
                cited_record_ids=[],
                has_unrecorded_gap=False,
                alternative_perspectives=[],
                actionable_suggestions=[
                    "生成仅限自己私密查看的客观事实时间线清单",
                    "起草不落把柄的职场书面留痕回复草稿",
                ],
            )
            return await self._finalize_and_persist_assistant_turn(
                prepared=prepared,
                raw_message=message,
                output=shaming_out,
                citations=(),
                prompt_version="sop_boundary_v0.2",
            )

        gateway = self._resolve_gateway()
        call_exc: Exception | None = None
        raw_out: CompanionChatOutput | None = None
        prompt_ver: str | None = None
        # ZERO DB pool hold during external LLM HTTP I/O (AiCallMeter persists in a 2ms post-call Tx)
        try:
            async with self._concurrency_slot(scope.user_id):
                raw_out, prompt_ver, _model_prof = await gateway.generate_companion_reply(
                    scope=scope.user_scope,
                    payload=ctx.chat_input,
                    session=None,
                )
        except Exception as exc:
            call_exc = exc
        if call_exc is not None:
            await self._release_failed_idempotency(
                scope,
                prepared.idem_row_id,
                idem_key=idem_key,
                idem_path=idem_path,
                user_message_id=prepared.user_msg_dto.id if prepared.user_msg_dto else None,
                thread_id=prepared.thread_id,
            )
            raise call_exc
        assert raw_out is not None

        cit_summary = process_complete_citations(
            raw_out.reply_text,
            ctx.allowed_record_ids,
            declared_cited_ids=raw_out.cited_record_ids,
            alias_to_record_id=ctx.alias_to_record_id,
        )
        cleaned_out = raw_out.model_copy(
            update={
                "reply_text": cit_summary.cleaned_text,
                "cited_record_ids": [c.record_id for c in cit_summary.citations],
            }
        )
        return await self._finalize_and_persist_assistant_turn(
            prepared=prepared,
            raw_message=message,
            output=cleaned_out,
            citations=cit_summary.citations,
            prompt_version=prompt_ver,
        )

    @staticmethod
    def _build_sse_done_payload(
        turn: ChatTurnResponse,
        *,
        writeback_queued: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messageId": str(turn.message_id),
            "tierSnapshot": turn.tier_snapshot.value,
            "hasUnrecordedGap": turn.has_unrecorded_gap,
            "citedMaterialIds": [str(mid) for mid in turn.cited_material_ids],
        }
        if writeback_queued:
            payload["writebackQueued"] = True
        return payload

    @staticmethod
    def _replay_turn_as_sse(turn: ChatTurnResponse) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            if turn.crisis_card is not None:
                yield format_sse_event(
                    "crisis",
                    turn.crisis_card.model_dump(mode="json", by_alias=True),
                )
                yield format_sse_event(
                    "done",
                    {
                        "messageId": str(turn.message_id),
                        "tierSnapshot": turn.tier_snapshot.value,
                        "hasUnrecordedGap": False,
                        "citedMaterialIds": [],
                    },
                )
                return

            asst = turn.assistant_message
            if asst.content:
                yield format_sse_event("delta", {"text": asst.content})
            for cit in asst.citations:
                yield format_sse_event(
                    "citation",
                    {
                        "index": cit.index,
                        "materialId": str(cit.material_id),
                        "title": cit.title,
                        "snippet": cit.snippet,
                        "happenedOn": cit.happened_on,
                    },
                )
            if asst.whisper_note:
                yield format_sse_event("whisper", {"text": asst.whisper_note})
            # ADR-005 #3: NEVER emit `perspective` SSE event for `silver` or `crystal` tiers
            if turn.tier_snapshot is Tier.GOLD and asst.perspectives:
                yield format_sse_event("perspective", {"items": asst.perspectives})
            if asst.suggestions:
                yield format_sse_event("suggestion", {"items": asst.suggestions})
            writeback_queued = should_writeback_chat_message(
                turn.user_message.content,
                asst.safety_category,
            )
            yield format_sse_event(
                "done",
                CompanionService._build_sse_done_payload(
                    turn,
                    writeback_queued=writeback_queued,
                ),
            )

        return _gen()

    @staticmethod
    def _parse_optional_action_tag(
        tail_tags: str,
        *,
        ctx: BuiltCompanionContext,
        local_today: date,
        safety_category: SafetyCategory,
    ) -> dict[str, Any] | None:
        """Deterministically validate an optional `<action>{...}</action>` JSON proposal."""
        if safety_category is not SafetyCategory.NORMAL:
            return None
        match = _TAGGED_ACTION_RE.search(tail_tags)
        if match is None:
            return None
        raw_block = match.group(1).strip()
        if not raw_block:
            return None
        try:
            parsed = json.loads(raw_block)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        kind = str(parsed.get("kind") or "").strip()
        if kind == "create_reminder":
            title = _clamp_clean_text(str(parsed.get("title") or ""), 36, tier=ctx.tier)
            if not title:
                return None
            raw_td = str(parsed.get("triggerDate") or parsed.get("trigger_date") or "").strip()
            try:
                trig_date = date.fromisoformat(raw_td[:10]) if raw_td else None
            except ValueError:
                trig_date = None
            if trig_date is None or trig_date < local_today:
                trig_date = local_today + timedelta(days=1)
            return {
                "id": str(new_id()),
                "kind": "create_reminder",
                "label": _clamp_clean_text(
                    str(parsed.get("label") or "提前设一个轻提醒"), 24, tier=ctx.tier
                )
                or "提前设一个轻提醒",
                "title": title,
                "triggerDate": trig_date.isoformat(),
                "whenLabel": _format_cn_month_day(trig_date),
            }
        if kind == "save_note":
            title = _clamp_clean_text(str(parsed.get("title") or ""), 36, tier=ctx.tier)
            body = _clamp_clean_text(str(parsed.get("body") or ""), 220, tier=ctx.tier)
            if not title or not body:
                return None
            return {
                "id": str(new_id()),
                "kind": "save_note",
                "label": _clamp_clean_text(
                    str(parsed.get("label") or "存进手帐"), 24, tier=ctx.tier
                )
                or "存进手帐",
                "title": title,
                "body": body,
            }
        return None

    async def stream_chat(
        self,
        *,
        scope: PersonScope,
        message: str,
        referenced_material_ids: Sequence[UUID] = (),
        client_message_id: str | None = None,
        idem_key: str | None = None,
        idem_path: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        """Stream Cupid companion chat with live calm `agent_step` trace & `action_proposal` cards."""
        return await self.prepare_sse_stream(
            scope=scope,
            message=message,
            referenced_material_ids=referenced_material_ids,
            client_message_id=client_message_id,
            idem_key=idem_key,
            idem_path=idem_path,
            emit_agent_steps=True,
            is_disconnected=is_disconnected,
        )

    async def prepare_sse_stream(
        self,
        *,
        scope: PersonScope,
        message: str,
        referenced_material_ids: Sequence[UUID] = (),
        client_message_id: str | None = None,
        idem_key: str | None = None,
        idem_path: str,
        emit_agent_steps: bool = False,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        """Validate & prepare chat turn synchronously, then return async SSE frame iterator.

        Router endpoints invoke `stream_chat()` (`emit_agent_steps=True`), emitting calm `agent_step`
        frames before opening the LLM stream. Direct callers using `prepare_sse_stream()` without
        `emit_agent_steps=True` preserve `delta` as the first yielded frame for backward compatibility.
        """
        prepared = await self._prepare_turn(
            scope=scope,
            message=message,
            referenced_material_ids=referenced_material_ids,
            client_message_id=client_message_id,
            idem_key=idem_key,
            idem_path=idem_path,
        )
        if prepared.replayed_turn is not None:
            return self._replay_turn_as_sse(prepared.replayed_turn)
        if prepared.completed_crisis_turn is not None:
            return self._replay_turn_as_sse(prepared.completed_crisis_turn)

        # Deterministic public shaming/doxxing refusal (Crisis SOP §1.2 Boundary 1)
        if is_public_shaming_request(message):
            shaming_out = CompanionChatOutput(
                safety_flag=SafetyCategory.BOUNDARY_REFUSAL,
                reply_text=PUBLIC_SHAMING_BOUNDARY_GUIDANCE,
                whisper_note="记录仅自己可见，重要沟通建议保留客观书面留痕",
                cited_record_ids=[],
                has_unrecorded_gap=False,
                alternative_perspectives=[],
                actionable_suggestions=[
                    "生成仅限自己私密查看的客观事实时间线清单",
                    "起草不落把柄的职场书面留痕回复草稿",
                ],
            )
            turn = await self._finalize_and_persist_assistant_turn(
                prepared=prepared,
                raw_message=message,
                output=shaming_out,
                citations=(),
                prompt_version="sop_boundary_v0.2",
            )
            return self._replay_turn_as_sse(turn)

        gateway = self._resolve_gateway()
        heartbeat_sec = max(
            0.02,
            float(self._container.extras.get("sse_heartbeat_seconds", 10.0)),
        )
        chunk_timeout_sec = max(
            heartbeat_sec,
            float(self._container.extras.get("sse_chunk_timeout_seconds", 25.0)),
        )

        async def _stream_gen() -> AsyncIterator[str]:
            assert prepared.context is not None
            assert prepared.safety_decision is not None
            ctx = prepared.context

            reply_extractor = StreamingReplyExtractor()
            citation_proc = StreamingCitationProcessor(
                ctx.allowed_record_ids,
                alias_to_record_id=ctx.alias_to_record_id,
            )
            sentence_guard = SentenceBufferedStreamGuard(tier=ctx.tier)
            emitted_chunks: list[str] = []
            reply_stream_finalized = False
            stream_completed = False
            stream_released = False
            prompt_ver: str | None = "ask_relationship_stream"
            sidecar_output: CompanionChatOutput | None = None

            def _citation_sse(cit: VerifiedCitation) -> str | None:
                meta = ctx.record_catalog.get(cit.record_id)
                if meta is None:
                    return None
                return format_sse_event(
                    "citation",
                    {
                        "index": cit.index,
                        "materialId": str(cit.record_id),
                        "title": meta.title,
                        "snippet": meta.snippet,
                        "happenedOn": meta.happened_on,
                    },
                )

            try:
                # 1. Stream calm Agent Execution Trace BEFORE opening LLM stream (NEVER on crisis!)
                if emit_agent_steps:
                    for step_payload in _build_agent_step_events(ctx):
                        yield format_sse_event("agent_step", step_payload)

                # On SafetyCategory.ILLEGAL_INTENT, immediately emit SOP §1.2 protective pull-back
                if prepared.safety_decision.category is SafetyCategory.ILLEGAL_INTENT:
                    pullback_chunk = f"{ILLEGAL_INTENT_ALLY_PULLBACK}\n\n"
                    emitted_chunks.append(pullback_chunk)
                    yield format_sse_event("delta", {"text": pullback_chunk})

                # ZERO DB session open during LLM streaming (BC3 BUG-CC-2)!
                async with self._concurrency_slot(scope.user_id):
                    try:
                        maybe_iter: Any = gateway.stream_companion_reply(
                            scope=scope.user_scope,
                            payload=ctx.chat_input,
                            session=None,
                        )
                        stream_iter: AsyncIterator[str] = (
                            await maybe_iter if inspect.isawaitable(maybe_iter) else maybe_iter
                        )
                        aiter_fn = getattr(stream_iter, "__aiter__", None)
                        anext_iter: Any = aiter_fn() if callable(aiter_fn) else stream_iter
                        next_task: asyncio.Task[str] | None = None
                        try:
                            while True:
                                next_task = asyncio.create_task(anext_iter.__anext__())
                                idle_started = time.monotonic()
                                while not next_task.done():
                                    done_set, _ = await asyncio.wait(
                                        {next_task},
                                        timeout=heartbeat_sec,
                                    )
                                    if done_set:
                                        break
                                    if is_disconnected is not None and await is_disconnected():
                                        next_task.cancel()
                                        with contextlib.suppress(BaseException):
                                            await next_task
                                        next_task = None
                                        raise asyncio.CancelledError(
                                            "Client disconnected during SSE stream"
                                        )
                                    if (time.monotonic() - idle_started) >= chunk_timeout_sec:
                                        next_task.cancel()
                                        with contextlib.suppress(BaseException):
                                            await next_task
                                        next_task = None
                                        raise TimeoutError(
                                            "Upstream LLM stream chunk idle timeout exceeded"
                                        )
                                    yield ": keep-alive\n\n"
                                try:
                                    raw_chunk = next_task.result()
                                except StopAsyncIteration:
                                    break
                                finally:
                                    next_task = None

                                if not raw_chunk:
                                    continue

                                decoded_body = reply_extractor.feed(raw_chunk)
                                if decoded_body:
                                    step = citation_proc.feed(decoded_body)
                                    safe_delta = sentence_guard.feed(step.delta_text)
                                    if safe_delta:
                                        emitted_chunks.append(safe_delta)
                                        yield format_sse_event("delta", {"text": safe_delta})

                                    for cit in step.new_citations:
                                        cit_frame = _citation_sse(cit)
                                        if cit_frame is not None:
                                            yield cit_frame

                                if reply_extractor.reply_closed and not reply_stream_finalized:
                                    reply_stream_finalized = True
                                    fin_cit = citation_proc.finalize()
                                    tail_safe = (
                                        sentence_guard.feed(fin_cit.delta_text)
                                        + sentence_guard.finalize()
                                    )
                                    if tail_safe:
                                        emitted_chunks.append(tail_safe)
                                        yield format_sse_event("delta", {"text": tail_safe})
                                    for cit in fin_cit.new_citations:
                                        cit_frame = _citation_sse(cit)
                                        if cit_frame is not None:
                                            yield cit_frame
                        finally:
                            if next_task is not None and not next_task.done():
                                next_task.cancel()
                                with contextlib.suppress(BaseException):
                                    await next_task
                            aclose_fn = getattr(anext_iter, "aclose", None)
                            if callable(aclose_fn):
                                with contextlib.suppress(Exception):
                                    await aclose_fn()

                        sidecar_output = getattr(stream_iter, "companion_output", None) or getattr(
                            gateway, "last_companion_output", None
                        )
                    except NotImplementedError:
                        gen_out, prompt_ver, _m = await gateway.generate_companion_reply(
                            scope=scope.user_scope,
                            payload=ctx.chat_input,
                            session=None,
                        )
                        sidecar_output = gen_out
                        step = citation_proc.feed(gen_out.reply_text)
                        safe_delta = sentence_guard.feed(step.delta_text)
                        if safe_delta:
                            emitted_chunks.append(safe_delta)
                            yield format_sse_event("delta", {"text": safe_delta})
                        for cit in step.new_citations:
                            cit_frame = _citation_sse(cit)
                            if cit_frame is not None:
                                yield cit_frame

                if not reply_stream_finalized:
                    reply_stream_finalized = True
                    rem_body = reply_extractor.finalize()
                    step_rem = citation_proc.feed(rem_body)
                    fin_step = citation_proc.finalize()
                    tail_delta = (
                        sentence_guard.feed(step_rem.delta_text + fin_step.delta_text)
                        + sentence_guard.finalize()
                    )
                    if tail_delta:
                        emitted_chunks.append(tail_delta)
                        yield format_sse_event("delta", {"text": tail_delta})

                    for cit in (*step_rem.new_citations, *fin_step.new_citations):
                        cit_frame = _citation_sse(cit)
                        if cit_frame is not None:
                            yield cit_frame

                assembled_reply = "".join(emitted_chunks).strip()
                if not assembled_reply:
                    assembled_reply = (
                        SILVER_LOYAL_FALLBACK_REPLY
                        if ctx.tier is Tier.SILVER
                        else FALLBACK_SAFE_REPLY
                    )
                    yield format_sse_event("delta", {"text": assembled_reply})

                has_any_ctx = bool(
                    ctx.chat_input.confirmed_facts or ctx.chat_input.referenced_materials
                )
                llm_action_proposal: dict[str, Any] | None = None
                if reply_extractor.used_xml_tags and sidecar_output is None:
                    tail_tags = reply_extractor.tail_text
                    w_match = _TAGGED_WHISPER_RE.search(tail_tags)
                    p_match = _TAGGED_PERSPECTIVES_RE.search(tail_tags)
                    s_match = _TAGGED_SUGGESTIONS_RE.search(tail_tags)
                    if w_match or p_match or s_match:
                        sidecar_output = CompanionChatOutput(
                            safety_flag=prepared.safety_decision.category,
                            reply_text=assembled_reply,
                            whisper_note=w_match.group(1).strip() if w_match else None,
                            cited_record_ids=citation_proc.verified_record_ids,
                            has_unrecorded_gap=detect_unrecorded_gap(
                                assembled_reply,
                                has_any_context=has_any_ctx,
                            ),
                            alternative_perspectives=(
                                _split_tag_items(p_match.group(1)) if p_match else []
                            ),
                            actionable_suggestions=(
                                _split_tag_items(s_match.group(1)) if s_match else []
                            ),
                        )
                elif reply_extractor.used_json_fallback and sidecar_output is None:
                    parsed_json = reply_extractor.parsed_json_payload()
                    if parsed_json is not None:
                        raw_whisper = parsed_json.get("whisper_note")
                        raw_persp = parsed_json.get("alternative_perspectives")
                        raw_sugg = parsed_json.get("actionable_suggestions")
                        raw_cits = parsed_json.get("cited_record_ids")
                        parsed_cit_ids: list[UUID] = []
                        if isinstance(raw_cits, list):
                            for cid in raw_cits:
                                try:
                                    parsed_cit_ids.append(UUID(str(cid)))
                                except ValueError:
                                    continue
                        sidecar_output = CompanionChatOutput(
                            safety_flag=prepared.safety_decision.category,
                            reply_text=assembled_reply,
                            whisper_note=(
                                str(raw_whisper).strip()
                                if isinstance(raw_whisper, str) and raw_whisper.strip()
                                else None
                            ),
                            cited_record_ids=parsed_cit_ids or citation_proc.verified_record_ids,
                            has_unrecorded_gap=bool(parsed_json.get("has_unrecorded_gap"))
                            or detect_unrecorded_gap(
                                assembled_reply,
                                has_any_context=has_any_ctx,
                            ),
                            alternative_perspectives=[
                                str(item).strip()
                                for item in (raw_persp if isinstance(raw_persp, list) else [])
                                if str(item).strip()
                            ],
                            actionable_suggestions=[
                                str(item).strip()
                                for item in (raw_sugg if isinstance(raw_sugg, list) else [])
                                if str(item).strip()
                            ],
                        )

                if isinstance(sidecar_output, CompanionChatOutput):
                    extra_cits = citation_proc.merge_declared_citations(
                        sidecar_output.cited_record_ids
                    )
                    for cit in extra_cits:
                        cit_frame = _citation_sse(cit)
                        if cit_frame is not None:
                            yield cit_frame
                    candidate_output = sidecar_output.model_copy(
                        update={
                            "reply_text": assembled_reply,
                            "cited_record_ids": citation_proc.verified_record_ids,
                        }
                    )
                else:
                    candidate_output = derive_stream_supplements(
                        ctx.chat_input,
                        cleaned_reply=assembled_reply,
                        verified_record_ids=citation_proc.verified_record_ids,
                        safety_category=prepared.safety_decision.category,
                    )

                persisted_turn = await self._finalize_and_persist_assistant_turn(
                    prepared=prepared,
                    raw_message=message,
                    output=candidate_output,
                    citations=citation_proc.verified_citations,
                    prompt_version=prompt_ver,
                )
                stream_completed = True

                asst = persisted_turn.assistant_message
                if asst.whisper_note:
                    yield format_sse_event("whisper", {"text": asst.whisper_note})

                # ONLY emit `event: perspective` when tier == "gold" AND non-empty!
                if persisted_turn.tier_snapshot is Tier.GOLD and asst.perspectives:
                    yield format_sse_event("perspective", {"items": asst.perspectives})

                if asst.suggestions:
                    yield format_sse_event("suggestion", {"items": asst.suggestions})

                # 2. Emit contextually grounded 1-tap executable action proposals (NEVER on crisis/refusals)
                local_today = to_user_local(self._container.clock.now(), ctx.timezone).date()
                if reply_extractor.tail_text:
                    llm_action_proposal = self._parse_optional_action_tag(
                        reply_extractor.tail_text,
                        ctx=ctx,
                        local_today=local_today,
                        safety_category=asst.safety_category,
                    )
                derived_proposals = _derive_action_proposals(
                    ctx,
                    user_message=message,
                    assistant_reply=asst.content,
                    local_today=local_today,
                    safety_category=asst.safety_category,
                    has_unrecorded_gap=asst.has_unrecorded_gap,
                )
                if llm_action_proposal is not None and not any(
                    p.get("kind") == llm_action_proposal.get("kind") for p in derived_proposals
                ):
                    derived_proposals = [llm_action_proposal, *derived_proposals][:2]
                for proposal in derived_proposals:
                    yield format_sse_event("action_proposal", proposal)

                writeback_queued = should_writeback_chat_message(
                    message,
                    asst.safety_category,
                )
                yield format_sse_event(
                    "done",
                    CompanionService._build_sse_done_payload(
                        persisted_turn,
                        writeback_queued=writeback_queued,
                    ),
                )
            except AppError as exc:
                await self._release_failed_idempotency(
                    scope,
                    prepared.idem_row_id,
                    idem_key=idem_key,
                    idem_path=idem_path,
                    user_message_id=prepared.user_msg_dto.id if prepared.user_msg_dto else None,
                    thread_id=prepared.thread_id,
                )
                stream_released = True
                yield format_sse_event(
                    "error",
                    {"code": exc.error_code, "detail": exc.detail},
                )
            except Exception:
                await self._release_failed_idempotency(
                    scope,
                    prepared.idem_row_id,
                    idem_key=idem_key,
                    idem_path=idem_path,
                    user_message_id=prepared.user_msg_dto.id if prepared.user_msg_dto else None,
                    thread_id=prepared.thread_id,
                )
                stream_released = True
                yield format_sse_event(
                    "error",
                    {
                        "code": "ai_upstream_error",
                        "detail": "Companion stream interrupted; please retry.",
                    },
                )
            finally:
                if not stream_completed and not stream_released:
                    await self._release_failed_idempotency(
                        scope,
                        prepared.idem_row_id,
                        idem_key=idem_key,
                        idem_path=idem_path,
                        user_message_id=(
                            prepared.user_msg_dto.id if prepared.user_msg_dto else None
                        ),
                        thread_id=prepared.thread_id,
                    )
                    stream_released = True

        return _stream_gen()

    async def get_person_brief(
        self,
        *,
        scope: PersonScope,
    ) -> PersonBriefResult:
        """Compute a calm, zero-guilt, tier-aware proactive relationship brief under PersonScope + FORCE RLS."""
        now_utc = self._container.clock.now()
        async with self._container.db.user_session(scope.user_scope) as session:
            person_row = await session.scalar(
                select(PersonRow).where(
                    PersonRow.user_id == scope.user_id,
                    PersonRow.id == scope.person_id,
                    PersonRow.deleted_at.is_(None),
                    PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
                )
            )
            if person_row is None:
                raise NotFoundError("Person not found")

            dek = await self._container.load_user_dek(session, scope.user_scope)
            tier = Tier.from_any(person_row.tier)
            raw_person_name = self._container.cipher.decrypt_str(
                dek,
                person_row.display_name_enc,
                user_id=scope.user_id,
                table="persons",
                column="display_name_enc",
                row_id=person_row.id,
            )
            person_name = (
                sanitize_text(raw_person_name, allow_empty=True).cleaned_text or "这位朋友"
            )

            pref_row = await session.scalar(
                select(UserPreferenceRow).where(UserPreferenceRow.user_id == scope.user_id)
            )
            tz_name = (
                pref_row.timezone
                if pref_row is not None
                else self._container.settings.default_timezone
            )
            local_today = to_user_local(now_utc, tz_name).date()

            # 1. Exact count of non-deleted, non-held records strictly for this person
            record_count = int(
                await session.scalar(
                    select(func.count(RecordRow.id)).where(
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                        RecordRow.deleted_at.is_(None),
                        RecordRow.review_state != RecordReviewState.HELD.value,
                    )
                )
                or 0
            )

            # 2. Active & proposed reminders for ONLY this person (excluding venting/held/deleted/rejected/superseded backing facts)
            reminder_rows = (
                await session.scalars(
                    select(ReminderRow)
                    .outerjoin(
                        FactRow,
                        and_(
                            FactRow.id == ReminderRow.fact_id,
                            FactRow.user_id == scope.user_id,
                            FactRow.person_id == scope.person_id,
                        ),
                    )
                    .outerjoin(
                        RecordRow,
                        and_(
                            RecordRow.id == FactRow.record_id,
                            RecordRow.user_id == scope.user_id,
                            RecordRow.person_id == scope.person_id,
                        ),
                    )
                    .where(
                        ReminderRow.user_id == scope.user_id,
                        ReminderRow.person_id == scope.person_id,
                        ReminderRow.status.in_(
                            (ReminderStatus.ACTIVE.value, ReminderStatus.PROPOSED.value)
                        ),
                        or_(
                            ReminderRow.fact_id.is_(None),
                            and_(
                                FactRow.id.is_not(None),
                                FactRow.invalid_at.is_(None),
                                FactRow.status != FactStatus.REJECTED.value,
                                FactRow.pending_reason.is_distinct_from(
                                    PendingReason.VENTING.value
                                ),
                                or_(
                                    FactRow.record_id.is_(None),
                                    and_(
                                        RecordRow.id.is_not(None),
                                        RecordRow.deleted_at.is_(None),
                                        RecordRow.review_state != RecordReviewState.HELD.value,
                                    ),
                                ),
                            ),
                        ),
                    )
                    .order_by(
                        ReminderRow.next_fire_at.asc().nullslast(),
                        ReminderRow.created_at.desc(),
                    )
                    .limit(30)
                )
            ).all()

            # 3. Pending D1 pencil facts (pending non-venting OR confirmed needs_glance=True)
            pencil_stmt = (
                select(FactRow, RecordRow)
                .outerjoin(
                    RecordRow,
                    and_(
                        FactRow.record_id == RecordRow.id,
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                    ),
                )
                .where(
                    FactRow.user_id == scope.user_id,
                    FactRow.person_id == scope.person_id,
                    FactRow.invalid_at.is_(None),
                    or_(
                        and_(
                            FactRow.status == FactStatus.PENDING.value,
                            FactRow.pending_reason.is_distinct_from(PendingReason.VENTING.value),
                        ),
                        and_(
                            FactRow.status == FactStatus.CONFIRMED.value,
                            FactRow.needs_glance.is_(True),
                        ),
                    ),
                    or_(
                        FactRow.record_id.is_(None),
                        and_(
                            RecordRow.id.is_not(None),
                            RecordRow.deleted_at.is_(None),
                            RecordRow.review_state != RecordReviewState.HELD.value,
                        ),
                    ),
                )
                .order_by(FactRow.created_at.desc(), FactRow.id.desc())
                .limit(20)
            )
            pencil_pairs = (await session.execute(pencil_stmt)).all()

            # 4. Confirmed active facts for ONLY this person (excluding deleted/held records)
            confirmed_stmt = (
                select(FactRow, RecordRow)
                .outerjoin(
                    RecordRow,
                    and_(
                        FactRow.record_id == RecordRow.id,
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                    ),
                )
                .where(
                    FactRow.user_id == scope.user_id,
                    FactRow.person_id == scope.person_id,
                    FactRow.status == FactStatus.CONFIRMED.value,
                    FactRow.invalid_at.is_(None),
                    or_(
                        FactRow.record_id.is_(None),
                        and_(
                            RecordRow.id.is_not(None),
                            RecordRow.deleted_at.is_(None),
                            RecordRow.review_state != RecordReviewState.HELD.value,
                        ),
                    ),
                )
                .order_by(FactRow.created_at.desc(), FactRow.id.desc())
                .limit(30)
            )
            confirmed_pairs = (await session.execute(confirmed_stmt)).all()

            fact_to_record_id: dict[UUID, UUID | None] = {}
            for f_row, r_row in (*pencil_pairs, *confirmed_pairs):
                fact_to_record_id[f_row.id] = r_row.id if r_row is not None else f_row.record_id

            # Decrypt reminders & resolve upcoming dates
            @dataclass(slots=True)
            class _BriefRem:
                title: str
                occ_date: date | None
                days_until: int | None
                when_label: str | None
                is_lunar: bool
                record_id: UUID | None

            upcoming_rems: list[_BriefRem] = []
            for rem in reminder_rows:
                raw_title = self._container.cipher.decrypt_str(
                    dek,
                    rem.title_enc,
                    user_id=scope.user_id,
                    table="reminders",
                    column="title_enc",
                    row_id=rem.id,
                )
                clean_title = _clamp_clean_text(raw_title, 48, tier=tier)
                if not clean_title:
                    continue

                occ_date: date | None = rem.once_date
                if occ_date is None and rem.next_fire_at is not None:
                    local_fire = to_user_local(rem.next_fire_at, tz_name).date()
                    occ_date = local_fire + timedelta(days=int(rem.next_lead_day or 0))
                elif occ_date is None and rem.target_month and rem.target_day:
                    try:
                        if rem.is_lunar:
                            occ_date = resolve_next_lunar_occurrence_date(
                                target_month=rem.target_month,
                                target_day=rem.target_day,
                                is_leap_month=bool(rem.is_leap_month),
                                timezone=tz_name,
                                reference_utc=now_utc,
                                fire_hour=rem.fire_hour,
                                fire_minute=rem.fire_minute,
                                lead_days=rem.lead_days or [0],
                            )
                        else:
                            cand_this = resolve_solar_yearly_date(
                                local_today.year, rem.target_month, rem.target_day
                            )
                            occ_date = (
                                cand_this
                                if cand_this >= local_today
                                else resolve_solar_yearly_date(
                                    local_today.year + 1, rem.target_month, rem.target_day
                                )
                            )
                    except ReminderScheduleError:
                        occ_date = None

                days_until = (occ_date - local_today).days if occ_date is not None else None
                if days_until is not None and days_until < 0:
                    continue

                when_label = format_fact_date_label(
                    trigger_date=occ_date,
                    is_lunar=bool(rem.is_lunar),
                    lunar_month=rem.target_month,
                    lunar_day=rem.target_day,
                )
                linked_rec = fact_to_record_id.get(rem.fact_id) if rem.fact_id is not None else None
                upcoming_rems.append(
                    _BriefRem(
                        title=clean_title,
                        occ_date=occ_date,
                        days_until=days_until,
                        when_label=when_label,
                        is_lunar=bool(rem.is_lunar),
                        record_id=linked_rec,
                    )
                )

            upcoming_rems.sort(
                key=lambda item: (
                    item.days_until is None,
                    item.days_until if item.days_until is not None else 999999,
                )
            )
            upcoming_within_14 = [
                r for r in upcoming_rems if r.days_until is not None and 0 <= r.days_until <= 14
            ]

            # Decrypt pending D1 pencil facts
            @dataclass(slots=True)
            class _BriefFact:
                content: str
                fact_type: str
                when_label: str | None
                is_lunar: bool
                record_id: UUID | None

            pencil_facts: list[_BriefFact] = []
            for f_row, r_row in pencil_pairs:
                raw_content = self._container.cipher.decrypt_str(
                    dek,
                    f_row.content_enc,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=f_row.id,
                )
                clean_content = _clamp_clean_text(raw_content, 48, tier=tier)
                if not clean_content:
                    continue
                when_lbl = format_fact_date_label(
                    trigger_date=f_row.trigger_date
                    or (r_row.happened_on if r_row is not None else None),
                    is_lunar=bool(f_row.is_lunar),
                    lunar_month=f_row.lunar_month,
                    lunar_day=f_row.lunar_day,
                )
                pencil_facts.append(
                    _BriefFact(
                        content=clean_content,
                        fact_type=f_row.type,
                        when_label=when_lbl,
                        is_lunar=bool(f_row.is_lunar),
                        record_id=r_row.id if r_row is not None else f_row.record_id,
                    )
                )

            # Decrypt confirmed facts sorted by domain priority (taboo/date/promise first)
            confirmed_facts: list[_BriefFact] = []
            for f_row, r_row in confirmed_pairs:
                raw_content = self._container.cipher.decrypt_str(
                    dek,
                    f_row.content_enc,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=f_row.id,
                )
                clean_content = _clamp_clean_text(raw_content, 48, tier=tier)
                if not clean_content:
                    continue
                when_lbl = format_fact_date_label(
                    trigger_date=f_row.trigger_date
                    or (r_row.happened_on if r_row is not None else None),
                    is_lunar=bool(f_row.is_lunar),
                    lunar_month=f_row.lunar_month,
                    lunar_day=f_row.lunar_day,
                )
                confirmed_facts.append(
                    _BriefFact(
                        content=clean_content,
                        fact_type=f_row.type,
                        when_label=when_lbl,
                        is_lunar=bool(f_row.is_lunar),
                        record_id=r_row.id if r_row is not None else f_row.record_id,
                    )
                )
            confirmed_facts.sort(key=lambda item: _BRIEF_FACT_TYPE_ORDER.get(item.fact_type, 9))

        upcoming_count = len(upcoming_rems)
        pending_count = len(pencil_facts)

        # Build calm, zero-guilt, tier-aware headline / subline / prompt
        if tier is Tier.GOLD:
            if upcoming_within_14:
                top_rem = upcoming_within_14[0]
                assert top_rem.days_until is not None
                day_prefix = (
                    "就是今天"
                    if top_rem.days_until == 0
                    else (
                        "就是明天" if top_rem.days_until == 1 else f"还有 {top_rem.days_until} 天"
                    )
                )
                headline = f"{day_prefix} · {top_rem.title}"
                subline = (
                    f"另有 {pending_count} 条轻记待你扫一眼确认，手帐共收着 {record_count} 页日常细节。"
                    if pending_count > 0
                    else f"手帐里已收好关于「{person_name}」的 {record_count} 页细节，提前准备更从容。"
                )
                date_prefix = _brief_date_prefix(top_rem.when_label, top_rem.title)
                prompt = f"{date_prefix}{top_rem.title}，结合我记下的手帐，帮我列一份准备清单"
            elif pending_count > 0:
                has_lunar_pencil = any(pf.is_lunar for pf in pencil_facts)
                headline = (
                    f"有 {pending_count} 条农历日子轻记，点一下即可确认落墨"
                    if has_lunar_pencil
                    else f"有 {pending_count} 条手帐轻记，点一下即可确认落墨"
                )
                subline = f"已为「{person_name}」收好 {record_count} 页暖心手帐，确认后日程更安心。"
                prompt = f"结合我为「{person_name}」记下的手帐，最近有什么贴心细节值得提前留意？"
            elif record_count > 0 or confirmed_facts:
                headline = f"已为「{person_name}」留存 {record_count} 页暖心手帐"
                if confirmed_facts:
                    subline = (
                        f"记得「{confirmed_facts[0].content}」等 {len(confirmed_facts)} 条小细节。"
                    )
                else:
                    subline = "把在乎的日常细节安放在这里，想念或准备心意时随时翻看。"
                prompt = f"结合手帐里关于「{person_name}」的偏好与忌口，最近可以准备什么贴心小事？"
            else:
                headline = f"关于「{person_name}」的手帐刚翻开第一页"
                subline = "随口记下一句偏好、忌口或约定，日后随时替你温柔想起。"
                prompt = f"我想开始记录关于「{person_name}」的日常细节，可以从哪些小事记起？"

        elif tier is Tier.SILVER:
            # Strictly calm, factual, loyal — NEVER push reconciliation!
            headline = (
                f"已冷静留存 {record_count} 份客观事实与时间线记录"
                if record_count > 0
                else f"已为「{person_name}」建好私密边界记录页"
            )
            if upcoming_within_14:
                subline = f"近期有 {len(upcoming_within_14)} 项节点提醒，重要沟通建议保持书面留痕。"
            elif pending_count > 0:
                subline = f"另有 {pending_count} 条待确认要点，核对后将按时间线归档留存。"
            else:
                subline = "只留客观时间与原话，需要梳理时间线或书面留痕回复时随时叫我。"
            prompt = f"按时间线帮我整理和「{person_name}」的关键客观事实与书面留痕要点"

        else:  # Tier.CRYSTAL
            if upcoming_within_14:
                top_rem = upcoming_within_14[0]
                assert top_rem.days_until is not None
                day_prefix = (
                    "就是今天"
                    if top_rem.days_until == 0
                    else (
                        "就是明天" if top_rem.days_until == 1 else f"还有 {top_rem.days_until} 天"
                    )
                )
                headline = f"{day_prefix} · {top_rem.title}"
            else:
                headline = f"已收好关于「{person_name}」的要点与待办"
            subline = (
                f"手帐里有 {record_count} 条往来记录与 {len(confirmed_facts)} 项偏好要点，清爽省心。"
                if (record_count > 0 or confirmed_facts)
                else "随手记下往来忌口、档期或借还事项，日常相处礼数周全不内耗。"
            )
            prompt = f"帮我快速梳理关于「{person_name}」的要点、偏好与近期待办"

        # Assemble up to 3 concise actionable items
        items: list[PersonBriefItem] = []
        seen_keys: set[tuple[str, str]] = set()

        def _add_item(
            kind: Literal["reminder", "pencil", "fact"],
            when: str | None,
            text: str,
            record_id: UUID | None,
            action_prompt: str,
        ) -> None:
            if len(items) >= 3:
                return
            clean_txt = _clamp_clean_text(text, 48, tier=tier)
            clean_prompt = _clamp_clean_text(action_prompt, 80, tier=tier)
            if not clean_txt or (kind, clean_txt) in seen_keys:
                return
            seen_keys.add((kind, clean_txt))
            items.append(
                PersonBriefItem(
                    kind=kind,
                    when=when,
                    text=clean_txt,
                    record_id=record_id,
                    action_prompt=clean_prompt or prompt,
                )
            )

        if tier is Tier.SILVER:
            for brief_rem in upcoming_rems[:1]:
                _add_item(
                    "reminder",
                    brief_rem.when_label,
                    brief_rem.title,
                    brief_rem.record_id,
                    f"围绕「{brief_rem.title}」，帮我梳理关键时间节点与书面留痕要点",
                )
            for cf in confirmed_facts[:2]:
                _add_item(
                    "fact",
                    cf.when_label,
                    cf.content,
                    cf.record_id,
                    f"结合「{cf.content}」，按时间线整理客观事实与防雷回复要点",
                )
            for pf in pencil_facts[:1]:
                _add_item(
                    "pencil",
                    pf.when_label,
                    pf.content,
                    pf.record_id,
                    f"核对「{pf.content}」这条记录并纳入时间线要点",
                )
        else:
            for brief_rem in upcoming_rems[:2]:
                date_prefix = _brief_date_prefix(brief_rem.when_label, brief_rem.title)
                _add_item(
                    "reminder",
                    brief_rem.when_label,
                    brief_rem.title,
                    brief_rem.record_id,
                    f"{date_prefix}{brief_rem.title}，结合我记下的手帐，帮我列一份准备清单",
                )
            for pf in pencil_facts[:1]:
                _add_item(
                    "pencil",
                    pf.when_label,
                    pf.content,
                    pf.record_id,
                    f"关于「{pf.content}」，结合手帐帮我确认一下安排与需要注意的细节",
                )
            for cf in confirmed_facts:
                if len(items) >= 3:
                    break
                _add_item(
                    "fact",
                    cf.when_label,
                    cf.content,
                    cf.record_id,
                    f"围绕「{cf.content}」，帮我看看和「{person_name}」相处或准备时有什么讲究",
                )

        return PersonBriefResult(
            person_id=scope.person_id,
            tier=tier,
            upcoming_count=upcoming_count,
            pending_count=pending_count,
            record_count=record_count,
            headline=_clamp_clean_text(headline, 72, tier=tier),
            subline=_clamp_clean_text(subline, 96, tier=tier),
            prompt=_clamp_clean_text(prompt, 96, tier=tier),
            items=items,
        )
