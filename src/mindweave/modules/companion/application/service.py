"""Application use-case service for Cupid Companion Chat (REST & SSE)."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.ai.schemas.contracts import CompanionChatOutput, SafetyDecision
from mindweave.core.clock import to_user_local
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import AppError, UpstreamAIError, ValidationFailedError
from mindweave.core.idempotency import hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.orm import BackgroundJobRow, IdempotencyKeyRow, TelemetryEventRow
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
    StreamingOutputGuard,
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
from mindweave.shared.enums import ChatRole, SafetyCategory, Tier
from mindweave.shared.scopes import PersonScope
from mindweave.workers.ports import (
    TASK_SUMMARIZE_THREAD,
    TASK_WRITEBACK_CHAT_FACTS,
    JobQueuePort,
)


def format_sse_event(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


_TAGGED_REPLY_RE = re.compile(r"<reply>(.*?)</reply>", re.DOTALL | re.IGNORECASE)
_TAGGED_WHISPER_RE = re.compile(r"<whisper>(.*?)</whisper>", re.DOTALL | re.IGNORECASE)
_TAGGED_PERSPECTIVES_RE = re.compile(
    r"<perspectives>(.*?)</perspectives>", re.DOTALL | re.IGNORECASE
)
_TAGGED_SUGGESTIONS_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL | re.IGNORECASE)


def _split_tag_items(raw_block: str) -> list[str]:
    lines = [
        line.strip().lstrip("-•*0123456789.、 ")
        for line in raw_block.strip().splitlines()
        if line.strip()
    ]
    return [item for item in lines if item]


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
        self._ctx_builder = ContextBuilder(
            container.cipher,
            default_timezone=container.settings.default_timezone,
        )

    def _resolve_gateway(self) -> ModelGatewayPort:
        if self._explicit_gateway is not None:
            return self._explicit_gateway
        for key in ("model_gateway", "gateway", "ai_gateway"):
            candidate = self._container.extras.get(key)
            if candidate is not None:
                return candidate  # type: ignore[no-any-return]
        raise UpstreamAIError("AI Model Gateway is not configured")

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

    async def clear_history(self, *, scope: PersonScope) -> ClearChatHistoryResponse:
        now = self._container.clock.now()
        async with self._container.db.user_session(scope.user_scope) as session:
            deleted = await self._repo.clear_history(session, scope=scope, now=now)
        return ClearChatHistoryResponse(person_id=scope.person_id, deleted_count=deleted)

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

            # 2. Build person-isolated context (verifies PersonRow & referenced_material_ids ownership!)
            context = await self._ctx_builder.build(
                session,
                scope=scope,
                dek=dek,
                user_message=cleaned_msg,
                referenced_material_ids=referenced_material_ids,
            )

            # 3. Enforce daily chat quota BEFORE saving new user message
            await self._container.quotas.check_chat_quota(
                session,
                scope.user_scope,
                tz_name=context.timezone,
            )

            # 4. Update continuous session window & late-night care check on ChatThreadRow
            now = self._container.clock.now()
            thread = await self._repo.get_or_create_thread_for_update(session, scope, now=now)
            prev_msg_count = thread.message_count
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

            # 6. Persist user ChatMessageRow encrypted with DEK + AAD (Invariant I2 & I12)
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
                client_message_id=client_message_id,
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
                context = BuiltCompanionContext(
                    scope=context.scope,
                    person_name=context.person_name,
                    tier=context.tier,
                    person_state=context.person_state,
                    role_tag=context.role_tag,
                    traits=context.traits,
                    chat_input=updated_input,
                    allowed_record_ids=context.allowed_record_ids,
                    record_catalog=context.record_catalog,
                    timezone=context.timezone,
                    late_night_care_enabled=context.late_night_care_enabled,
                )

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
        sanitized, _report = sanitize_companion_output(policed)

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
        self, scope: PersonScope, idem_row_id: UUID | None
    ) -> None:
        if idem_row_id is None:
            return
        async with self._container.db.user_session(scope.user_scope) as session:
            await session.execute(
                delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.user_id == scope.user_id,
                    IdempotencyKeyRow.id == idem_row_id,
                    IdempotencyKeyRow.status == "in_progress",
                )
            )

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
                whisper_note="私密树洞只为你一个人守口如瓶，对外反击我们走最稳妥的合法留痕。",
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
        async with self._container.db.user_session(scope.user_scope) as meter_session:
            try:
                raw_out, prompt_ver, _model_prof = await gateway.generate_companion_reply(
                    scope=scope.user_scope,
                    payload=ctx.chat_input,
                    session=meter_session,
                )
            except Exception as exc:
                call_exc = exc
        if call_exc is not None:
            await self._release_failed_idempotency(scope, prepared.idem_row_id)
            raise call_exc
        assert raw_out is not None

        cit_summary = process_complete_citations(
            raw_out.reply_text,
            ctx.allowed_record_ids,
            declared_cited_ids=raw_out.cited_record_ids,
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
            if turn.tier_snapshot is Tier.GOLD and asst.perspectives:
                yield format_sse_event("perspective", {"items": asst.perspectives})
            if asst.suggestions:
                yield format_sse_event("suggestion", {"items": asst.suggestions})
            yield format_sse_event(
                "done",
                {
                    "messageId": str(turn.message_id),
                    "tierSnapshot": turn.tier_snapshot.value,
                    "hasUnrecordedGap": turn.has_unrecorded_gap,
                    "citedMaterialIds": [str(mid) for mid in turn.cited_material_ids],
                },
            )

        return _gen()

    async def prepare_sse_stream(
        self,
        *,
        scope: PersonScope,
        message: str,
        referenced_material_ids: Sequence[UUID] = (),
        client_message_id: str | None = None,
        idem_key: str | None = None,
        idem_path: str,
    ) -> AsyncIterator[str]:
        """Validate & prepare chat turn synchronously, then return async SSE frame iterator."""
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
                whisper_note="私密树洞只为你一个人守口如瓶，对外反击我们走最稳妥的合法留痕。",
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

        async def _stream_gen() -> AsyncIterator[str]:
            assert prepared.context is not None
            assert prepared.safety_decision is not None
            ctx = prepared.context

            citation_proc = StreamingCitationProcessor(ctx.allowed_record_ids)
            guard_stream = StreamingOutputGuard()
            emitted_chunks: list[str] = []
            raw_accum: list[str] = []
            mode_decided = False
            tagged_xml_mode = False
            prompt_ver: str | None = "ask_relationship_stream"
            sidecar_output: CompanionChatOutput | None = None

            try:
                # On SafetyCategory.ILLEGAL_INTENT, immediately emit SOP §1.2 protective pull-back
                if prepared.safety_decision.category is SafetyCategory.ILLEGAL_INTENT:
                    pullback_chunk = f"{ILLEGAL_INTENT_ALLY_PULLBACK}\n\n"
                    emitted_chunks.append(pullback_chunk)
                    yield format_sse_event("delta", {"text": pullback_chunk})

                stream_exc: Exception | None = None
                async with self._container.db.user_session(scope.user_scope) as meter_session:
                    try:
                        maybe_iter: Any = gateway.stream_companion_reply(
                            scope=scope.user_scope,
                            payload=ctx.chat_input,
                            session=meter_session,
                        )
                        stream_iter: AsyncIterator[str] = (
                            await maybe_iter if inspect.isawaitable(maybe_iter) else maybe_iter
                        )
                        async for raw_chunk in stream_iter:
                            if not raw_chunk:
                                continue
                            raw_accum.append(raw_chunk)

                            if not mode_decided:
                                joined_start = "".join(raw_accum).lstrip()
                                if not joined_start:
                                    continue
                                if len(joined_start) < 7 and "<reply>".startswith(
                                    joined_start.lower()
                                ):
                                    continue
                                mode_decided = True
                                if joined_start.lower().startswith("<reply>"):
                                    tagged_xml_mode = True
                                    continue
                                # Flush buffered initial plain-text chunks
                                to_feed = "".join(raw_accum)
                            else:
                                if tagged_xml_mode:
                                    continue
                                to_feed = raw_chunk

                            step = citation_proc.feed(to_feed)
                            safe_delta = guard_stream.feed(step.delta_text)
                            if safe_delta:
                                emitted_chunks.append(safe_delta)
                                yield format_sse_event("delta", {"text": safe_delta})

                            for cit in step.new_citations:
                                meta = ctx.record_catalog.get(cit.record_id)
                                if meta is not None:
                                    yield format_sse_event(
                                        "citation",
                                        {
                                            "index": cit.index,
                                            "materialId": str(cit.record_id),
                                            "title": meta.title,
                                            "snippet": meta.snippet,
                                            "happenedOn": meta.happened_on,
                                        },
                                    )

                        sidecar_output = getattr(stream_iter, "companion_output", None) or getattr(
                            gateway, "last_companion_output", None
                        )
                    except NotImplementedError:
                        gen_out, prompt_ver, _m = await gateway.generate_companion_reply(
                            scope=scope.user_scope,
                            payload=ctx.chat_input,
                            session=meter_session,
                        )
                        sidecar_output = gen_out
                        step = citation_proc.feed(gen_out.reply_text)
                        safe_delta = guard_stream.feed(step.delta_text)
                        if safe_delta:
                            emitted_chunks.append(safe_delta)
                            yield format_sse_event("delta", {"text": safe_delta})
                        for cit in step.new_citations:
                            meta = ctx.record_catalog.get(cit.record_id)
                            if meta is not None:
                                yield format_sse_event(
                                    "citation",
                                    {
                                        "index": cit.index,
                                        "materialId": str(cit.record_id),
                                        "title": meta.title,
                                        "snippet": meta.snippet,
                                        "happenedOn": meta.happened_on,
                                    },
                                )
                    except Exception as exc:
                        stream_exc = exc

                if stream_exc is not None:
                    raise stream_exc

                if tagged_xml_mode:
                    full_tagged = "".join(raw_accum)
                    reply_match = _TAGGED_REPLY_RE.search(full_tagged)
                    reply_body = reply_match.group(1) if reply_match else full_tagged
                    step1 = citation_proc.feed(reply_body)
                    step2 = citation_proc.finalize()
                    combined_delta = (
                        guard_stream.feed(step1.delta_text + step2.delta_text)
                        + guard_stream.finalize()
                    )
                    if combined_delta:
                        emitted_chunks.append(combined_delta)
                        yield format_sse_event("delta", {"text": combined_delta})
                    for cit in (*step1.new_citations, *step2.new_citations):
                        meta = ctx.record_catalog.get(cit.record_id)
                        if meta is not None:
                            yield format_sse_event(
                                "citation",
                                {
                                    "index": cit.index,
                                    "materialId": str(cit.record_id),
                                    "title": meta.title,
                                    "snippet": meta.snippet,
                                    "happenedOn": meta.happened_on,
                                },
                            )

                    w_match = _TAGGED_WHISPER_RE.search(full_tagged)
                    p_match = _TAGGED_PERSPECTIVES_RE.search(full_tagged)
                    s_match = _TAGGED_SUGGESTIONS_RE.search(full_tagged)
                    sidecar_output = CompanionChatOutput(
                        safety_flag=prepared.safety_decision.category,
                        reply_text="".join(emitted_chunks),
                        whisper_note=w_match.group(1).strip() if w_match else None,
                        cited_record_ids=citation_proc.verified_record_ids,
                        has_unrecorded_gap=not bool(
                            ctx.chat_input.confirmed_facts or ctx.chat_input.referenced_materials
                        ),
                        alternative_perspectives=(
                            _split_tag_items(p_match.group(1)) if p_match else []
                        ),
                        actionable_suggestions=(
                            _split_tag_items(s_match.group(1)) if s_match else []
                        ),
                    )
                else:
                    if not mode_decided and raw_accum:
                        step_rem = citation_proc.feed("".join(raw_accum))
                        safe_rem = guard_stream.feed(step_rem.delta_text)
                        if safe_rem:
                            emitted_chunks.append(safe_rem)
                            yield format_sse_event("delta", {"text": safe_rem})
                        for cit in step_rem.new_citations:
                            meta = ctx.record_catalog.get(cit.record_id)
                            if meta is not None:
                                yield format_sse_event(
                                    "citation",
                                    {
                                        "index": cit.index,
                                        "materialId": str(cit.record_id),
                                        "title": meta.title,
                                        "snippet": meta.snippet,
                                        "happenedOn": meta.happened_on,
                                    },
                                )

                    fin_step = citation_proc.finalize()
                    tail_delta = guard_stream.feed(fin_step.delta_text) + guard_stream.finalize()
                    if tail_delta:
                        emitted_chunks.append(tail_delta)
                        yield format_sse_event("delta", {"text": tail_delta})

                    for cit in fin_step.new_citations:
                        meta = ctx.record_catalog.get(cit.record_id)
                        if meta is not None:
                            yield format_sse_event(
                                "citation",
                                {
                                    "index": cit.index,
                                    "materialId": str(cit.record_id),
                                    "title": meta.title,
                                    "snippet": meta.snippet,
                                    "happenedOn": meta.happened_on,
                                },
                            )

                assembled_reply = "".join(emitted_chunks).strip()
                if not assembled_reply:
                    assembled_reply = FALLBACK_SAFE_REPLY
                    yield format_sse_event("delta", {"text": assembled_reply})

                if isinstance(sidecar_output, CompanionChatOutput):
                    extra_cits = citation_proc.merge_declared_citations(
                        sidecar_output.cited_record_ids
                    )
                    for cit in extra_cits:
                        meta = ctx.record_catalog.get(cit.record_id)
                        if meta is not None:
                            yield format_sse_event(
                                "citation",
                                {
                                    "index": cit.index,
                                    "materialId": str(cit.record_id),
                                    "title": meta.title,
                                    "snippet": meta.snippet,
                                    "happenedOn": meta.happened_on,
                                },
                            )
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

                asst = persisted_turn.assistant_message
                if asst.whisper_note:
                    yield format_sse_event("whisper", {"text": asst.whisper_note})

                # ONLY emit `event: perspective` when tier == "gold" AND non-empty!
                if persisted_turn.tier_snapshot is Tier.GOLD and asst.perspectives:
                    yield format_sse_event("perspective", {"items": asst.perspectives})

                if asst.suggestions:
                    yield format_sse_event("suggestion", {"items": asst.suggestions})

                yield format_sse_event(
                    "done",
                    {
                        "messageId": str(persisted_turn.message_id),
                        "tierSnapshot": persisted_turn.tier_snapshot.value,
                        "hasUnrecordedGap": persisted_turn.has_unrecorded_gap,
                        "citedMaterialIds": [str(mid) for mid in persisted_turn.cited_material_ids],
                    },
                )
            except AppError as exc:
                await self._release_failed_idempotency(scope, prepared.idem_row_id)
                yield format_sse_event(
                    "error",
                    {"code": exc.error_code, "detail": exc.detail},
                )
            except Exception:
                await self._release_failed_idempotency(scope, prepared.idem_row_id)
                yield format_sse_event(
                    "error",
                    {
                        "code": "ai_upstream_error",
                        "detail": "Companion stream interrupted; please retry.",
                    },
                )

        return _stream_gen()
