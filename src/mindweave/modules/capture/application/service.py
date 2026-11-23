"""Multimodal Capture Pipeline Application Service (Invariants I1, I2, I3, I5, I6, I11, I12)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select

from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.ai.schemas.contracts import (
    ExtractedFactItem,
    ExtractRecordInput,
    KnownPersonRef,
    SafetyDecision,
    TranscribeInput,
)
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import strip_forbidden_dev_jargon
from mindweave.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from mindweave.core.idempotency import IdempotencyHit, hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    FactRow,
    IdempotencyKeyRow,
    InboxItemRow,
    RecordRow,
)
from mindweave.modules.capture.application.dto import (
    CaptureResponse,
    CaptureSplitDto,
    CaptureUndoResponse,
    CrisisCardDto,
    VoiceIngestResponse,
)
from mindweave.modules.capture.domain.gate_policy import GateConfig, GateInput, decide_gate
from mindweave.modules.capture.domain.highlighter import (
    build_natural_slip_tag,
    build_transcript_html,
    format_duration_mmss,
)
from mindweave.modules.capture.domain.media_sniffer import (
    InvalidMediaFormatError,
    MediaTooLargeError,
    validate_and_sniff_audio,
)
from mindweave.modules.capture.domain.routing import (
    CandidateHint,
    KnownPersonProfile,
    apply_spoken_self_correction,
    is_non_person_stopword_mention,
    normalize_mention,
    partition_by_event_group,
    resolve_by_history_clues,
    resolve_mention_for_fact,
)
from mindweave.modules.capture.domain.screenshot_rules import (
    filter_screenshot_facts,
    format_screenshot_bubbles,
)
from mindweave.modules.capture.infra.image_sanitizer import (
    SanitizedImage,
    validate_and_strip_image_exif,
)
from mindweave.modules.capture.infra.repository import (
    get_or_create_unassigned_placeholder,
    load_known_person_profiles,
    load_recent_person_context,
    revert_capture_graph,
)
from mindweave.modules.memory.infra.pencil_views import (
    fact_row_to_pencil_view as _fact_row_to_pencil_view,
)
from mindweave.modules.memory.infra.pencil_views import (
    pending_reason_or_none as _pending_reason_or_none,
)
from mindweave.modules.memory.infra.pencil_views import (
    primary_inbox_id as _primary_inbox_id,
)
from mindweave.modules.memory.infra.reminders_helper import upsert_reminder_for_fact
from mindweave.modules.memory.infra.repository import (
    apply_bitemporal_supersession_for_fact,
    check_polarity_flip_for_person,
    create_person_with_primary_alias,
    get_user_timezone,
    require_person_row,
)
from mindweave.shared.enums import (
    CaptureSource,
    CaptureStatus,
    CreatedBy,
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    PendingReason,
    PersonState,
    RecordKind,
    RecordReviewState,
    ReminderStatus,
    RouteMode,
    SafetyCategory,
    SourceSpeaker,
    Tier,
)
from mindweave.shared.pencil import auto_ink_due_at, derive_pencil_state, is_auto_ink_eligible
from mindweave.shared.pii import mask_hard_pii
from mindweave.shared.scopes import UserScope
from mindweave.workers.ports import TASK_PROCESS_CAPTURE, JobQueuePort

logger = structlog.get_logger(__name__)

_TERMINAL_CAPTURE_STATUSES = frozenset(
    {
        CaptureStatus.COMPLETED.value,
        CaptureStatus.NEEDS_REVIEW.value,
        CaptureStatus.SAVED_AS_NOTE.value,
        CaptureStatus.REVERTED.value,
    }
)


def _safe_parse_iso_date(raw: str | None) -> date | None:
    if not raw or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


@dataclass(slots=True)
class CaptureIngestCommand:
    scope: UserScope
    source: CaptureSource
    route_mode: RouteMode
    active_person_id: UUID | None = None
    text: str | None = None
    audio_bytes: bytes | None = None
    image_bytes: bytes | None = None
    keep_original_media: bool = False
    sync: bool | None = None
    client_capture_id: UUID | None = None
    idem_key: str | None = None
    route_path: str = "/v1/captures"


@dataclass(slots=True)
class CapturePipelineService:
    container: AppContainer

    def _gate_config(self) -> GateConfig:
        return GateConfig(
            confidence_threshold=self.container.settings.gate_confidence_threshold,
            undo_window_seconds=self.container.settings.undo_window_seconds,
        )

    def _get_gateway(self) -> ModelGatewayPort | None:
        return cast(
            ModelGatewayPort | None,
            self.container.extras.get("gateway")
            or self.container.extras.get("model_gateway")
            or self.container.extras.get("ai_gateway"),
        )

    def _get_safety(self) -> SafetyServicePort | None:
        return cast(
            SafetyServicePort | None,
            self.container.extras.get("safety") or self.container.extras.get("safety_service"),
        )

    def _get_job_queue(self) -> JobQueuePort | None:
        return cast(
            JobQueuePort | None,
            self.container.extras.get("job_queue") or self.container.extras.get("queue"),
        )

    # =========================================================================
    # Main Entry Point: POST /captures & POST /voice/ingest
    # =========================================================================

    async def submit_capture(
        self,
        cmd: CaptureIngestCommand,
        *,
        force_sync: bool = False,
        response_serializer: Callable[[CaptureResponse], dict[str, Any]] | None = None,
    ) -> tuple[CaptureResponse | None, IdempotencyHit | None, int]:
        """Validate, durably commit `CaptureRow(status='received')`, then process inline or enqueue."""
        if cmd.route_mode is RouteMode.CURRENT and cmd.active_person_id is None:
            raise BadRequestError("activePersonId is required when routeMode is 'current'")

        # 1. Pre-flight media & payload validation
        audio_info = None
        sanitized_img: SanitizedImage | None = None

        if cmd.source is CaptureSource.VOICE:
            if not cmd.audio_bytes:
                raise ValidationFailedError("Voice capture requires non-empty audio file")
            try:
                audio_info = validate_and_sniff_audio(
                    cmd.audio_bytes,
                    max_bytes=self.container.settings.max_audio_bytes,
                )
            except MediaTooLargeError as exc:
                raise PayloadTooLargeError(str(exc)) from exc
            except InvalidMediaFormatError as exc:
                raise ValidationFailedError(str(exc)) from exc
        elif cmd.source is CaptureSource.IMAGE:
            if not cmd.image_bytes:
                raise ValidationFailedError("Screenshot capture requires non-empty image file")
            sanitized_img = validate_and_strip_image_exif(
                cmd.image_bytes,
                max_bytes=self.container.settings.max_image_bytes,
            )
        else:
            if not cmd.text or not cmd.text.strip():
                raise ValidationFailedError("Text capture requires non-empty text")

        job_queue = self._get_job_queue()
        if force_sync or cmd.sync is True or job_queue is None:
            run_inline = True
        elif cmd.sync is False:
            run_inline = False
        else:
            # Default when job_queue is wired and sync not specified: async queue
            run_inline = False

        effective_idem_key = cmd.idem_key or str(new_id())
        req_hash = hash_request_payload(
            {
                "routePath": cmd.route_path,
                "source": cmd.source.value,
                "routeMode": cmd.route_mode.value,
                "activePersonId": str(cmd.active_person_id) if cmd.active_person_id else None,
                "keepOriginalMedia": cmd.keep_original_media,
                "text": cmd.text or "",
                "audioLen": len(cmd.audio_bytes) if cmd.audio_bytes else 0,
                "imageLen": len(cmd.image_bytes) if cmd.image_bytes else 0,
                "sync": run_inline,
            }
        )

        keep_image = bool(cmd.keep_original_media) or (
            not self.container.settings.ephemeral_screenshot_by_default
        )

        # ---------------------------------------------------------------------
        # TRANSACTION 1 (Invariant I2): Persist CaptureRow(status="received")
        # and encrypted media/raw_text BEFORE calling any external AI port!
        # ---------------------------------------------------------------------
        capture_id = cmd.client_capture_id or new_id()
        idem_row_id: UUID | None = None
        ephemeral_image_key: str | None = None

        async with self.container.db.user_session(cmd.scope) as session:
            if cmd.idem_key is not None:
                idem_row, hit = await self.container.idempotency.claim_or_replay(
                    session,
                    scope=cmd.scope,
                    idem_key=cmd.idem_key,
                    method="POST",
                    path=cmd.route_path,
                    request_hash=req_hash,
                )
                if hit is not None:
                    return None, hit, hit.status_code
                assert idem_row is not None
                idem_row_id = idem_row.id

            if await session.get(CaptureRow, capture_id) is not None:
                raise ConflictError("Capture ID already exists")

            if cmd.active_person_id is not None:
                active_person = await require_person_row(session, cmd.scope, cmd.active_person_id)
                if (
                    active_person.archived_at is not None
                    or active_person.state == PersonState.ARCHIVED.value
                ):
                    raise ValidationFailedError("Cannot capture into an archived person")

            tz_name = await get_user_timezone(session, cmd.scope)
            if cmd.source is CaptureSource.VOICE:
                await self.container.quotas.check_voice_quota(session, cmd.scope, tz_name=tz_name)

            dek = await self.container.load_user_dek(session, cmd.scope)
            now = self.container.clock.now()

            audio_key: str | None = None
            image_key: str | None = None

            if cmd.source is CaptureSource.VOICE and cmd.audio_bytes and audio_info is not None:
                audio_key = (
                    f"{cmd.scope.user_id}/captures/{capture_id}/audio.{audio_info.extension}"
                )
                await self.container.storage.put_encrypted(
                    user_id=cmd.scope.user_id,
                    dek=dek,
                    object_key=audio_key,
                    plaintext=cmd.audio_bytes,
                    content_type=audio_info.mime_type,
                )
            elif cmd.source is CaptureSource.IMAGE and sanitized_img is not None:
                if keep_image:
                    image_key = f"{cmd.scope.user_id}/captures/{capture_id}/screenshot.{sanitized_img.extension}"
                    await self.container.storage.put_encrypted(
                        user_id=cmd.scope.user_id,
                        dek=dek,
                        object_key=image_key,
                        plaintext=sanitized_img.clean_bytes,
                        content_type=sanitized_img.mime_type,
                    )
                elif not run_inline:
                    # Stage temporary EXIF-stripped encrypted image for worker; deleted immediately after OCR
                    ephemeral_image_key = f"{cmd.scope.user_id}/captures/{capture_id}/_ephemeral.{sanitized_img.extension}"
                    await self.container.storage.put_encrypted(
                        user_id=cmd.scope.user_id,
                        dek=dek,
                        object_key=ephemeral_image_key,
                        plaintext=sanitized_img.clean_bytes,
                        content_type=sanitized_img.mime_type,
                    )

            raw_text_enc = (
                self.container.cipher.encrypt_str(
                    dek,
                    cmd.text,
                    user_id=cmd.scope.user_id,
                    table="captures",
                    column="raw_text_enc",
                    row_id=capture_id,
                )
                if cmd.text
                else None
            )

            capture_row = CaptureRow(
                id=capture_id,
                user_id=cmd.scope.user_id,
                source=cmd.source.value,
                route_mode=cmd.route_mode.value,
                scoped_person_id=cmd.active_person_id,
                status=CaptureStatus.RECEIVED.value,
                audio_object_key=audio_key,
                image_object_key=image_key,
                keep_original_media=keep_image if cmd.source is CaptureSource.IMAGE else True,
                raw_text_enc=raw_text_enc,
                safety_category=SafetyCategory.NORMAL.value,
                idempotency_key=effective_idem_key,
                created_at=now,
            )
            session.add(capture_row)
            await session.flush()

            if not run_inline:
                assert job_queue is not None
                await job_queue.enqueue(
                    session,
                    task_name=TASK_PROCESS_CAPTURE,
                    payload={
                        "capture_id": str(capture_id),
                        "user_id": str(cmd.scope.user_id),
                        "ephemeral_image_key": ephemeral_image_key,
                    },
                    user_id=cmd.scope.user_id,
                    idempotency_key=f"capture:{capture_id}",
                )
                queued_dto = CaptureResponse(
                    capture_id=capture_id,
                    source=cmd.source,
                    route_mode=cmd.route_mode,
                    scoped_person_id=cmd.active_person_id,
                    status=CaptureStatus.RECEIVED,
                    created_at=now,
                )
                if idem_row_id is not None:
                    idem_row_loaded = await session.get(IdempotencyKeyRow, idem_row_id)
                    if idem_row_loaded is not None:
                        body = (
                            response_serializer(queued_dto)
                            if response_serializer is not None
                            else queued_dto.model_dump(mode="json", by_alias=True)
                        )
                        await self.container.idempotency.complete(
                            session,
                            idem_row_loaded,
                            status_code=202,
                            response_body=body,
                        )
                return queued_dto, None, 202

        # ---------------------------------------------------------------------
        # Inline Synchronous Execution (Transaction 1 is already committed!)
        # ---------------------------------------------------------------------
        inline_ephemeral_img = (
            (sanitized_img.clean_bytes, sanitized_img.mime_type)
            if (cmd.source is CaptureSource.IMAGE and sanitized_img is not None and not keep_image)
            else None
        )
        result_dto = await self.process_capture_by_id(
            scope=cmd.scope,
            capture_id=capture_id,
            in_memory_ephemeral_image=inline_ephemeral_img,
            idem_row_id=idem_row_id,
            idem_status_code=200 if force_sync else 201,
            response_serializer=response_serializer,
        )
        return result_dto, None, (200 if force_sync else 201)

    @staticmethod
    def _to_v10_voice_response(cap_resp: CaptureResponse) -> VoiceIngestResponse:
        return VoiceIngestResponse(
            job_id=cap_resp.capture_id,
            route_mode=cap_resp.route_mode,
            transcript_html=cap_resp.transcript_html,
            splits=cap_resp.splits,
            pending_count=cap_resp.pending_count,
            undo_deadline=cap_resp.undo_deadline,
            crisis_card=cap_resp.crisis_card,
            inbox_item_ids=cap_resp.inbox_item_ids,
        )

    async def ingest_voice_v10(
        self,
        cmd: CaptureIngestCommand,
    ) -> tuple[VoiceIngestResponse | None, IdempotencyHit | None]:
        """Execute synchronous V10 `/voice/ingest` flow and format as `VoiceIngestResponse`."""
        cap_resp, hit, _ = await self.submit_capture(
            cmd,
            force_sync=True,
            response_serializer=lambda resp: self._to_v10_voice_response(resp).model_dump(
                mode="json", by_alias=True
            ),
        )
        if hit is not None:
            return None, hit
        assert cap_resp is not None
        return self._to_v10_voice_response(cap_resp), None

    # =========================================================================
    # Core Pipeline Worker / Inline Processor
    # =========================================================================

    async def process_capture_by_id(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        ephemeral_image_key: str | None = None,
        in_memory_ephemeral_image: tuple[bytes, str] | None = None,
        idem_row_id: UUID | None = None,
        idem_status_code: int = 201,
        response_serializer: Callable[[CaptureResponse], dict[str, Any]] | None = None,
    ) -> CaptureResponse:
        """Process a `received` capture into per-person records, gated facts, inbox items, and reminders."""
        # 1. Load capture metadata & known persons in a short read session
        async with self.container.db.user_session(scope) as session:
            capture = await session.scalar(
                select(CaptureRow).where(
                    CaptureRow.id == capture_id,
                    CaptureRow.user_id == scope.user_id,
                )
            )
            if capture is None:
                raise NotFoundError("Capture not found")
            if capture.status in _TERMINAL_CAPTURE_STATUSES:
                dek = await self.container.load_user_dek(session, scope)
                return self._capture_row_to_response(dek, capture)

            dek = await self.container.load_user_dek(session, scope)
            tz_name = await get_user_timezone(session, scope)
            known_profiles, _ = await load_known_person_profiles(
                session,
                scope=scope,
                dek=dek,
                cipher=self.container.cipher,
            )
            facts_by_person, recent_snippets = await load_recent_person_context(
                session,
                scope,
                dek,
                self.container.cipher,
                [p.person_id for p in known_profiles],
            )

            source = CaptureSource(capture.source)
            route_mode = RouteMode(capture.route_mode)
            scoped_person_id = capture.scoped_person_id
            audio_object_key = capture.audio_object_key
            image_object_key = capture.image_object_key
            raw_text = (
                self.container.cipher.decrypt_str(
                    dek,
                    capture.raw_text_enc,
                    user_id=scope.user_id,
                    table="captures",
                    column="raw_text_enc",
                    row_id=capture.id,
                )
                if capture.raw_text_enc is not None
                else ""
            )

        known_profiles = [
            replace(p, recent_facts=tuple(facts_by_person.get(p.person_id, ())))
            for p in known_profiles
        ]
        profiles_by_id = {p.person_id: p for p in known_profiles}
        scoped_profile = profiles_by_id.get(scoped_person_id) if scoped_person_id else None

        gateway = self._get_gateway()
        safety_svc = self._get_safety()

        effective_text = raw_text
        duration_ms: int | None = None
        screenshot_is_group = False
        safety_decision = SafetyDecision(category=SafetyCategory.NORMAL)

        try:
            # 2a. Modality ingestion (ASR or Screenshot Bubble OCR)
            if source is CaptureSource.VOICE:
                if gateway is None:
                    raise RuntimeError("AI ModelGatewayPort is not configured")
                if not audio_object_key:
                    raise RuntimeError("Missing audio_object_key on voice capture")
                audio_bytes, audio_mime = await self.container.storage.get_decrypted(
                    user_id=scope.user_id,
                    dek=dek,
                    object_key=audio_object_key,
                )
                known_names: list[str] = []
                for p in known_profiles:
                    known_names.extend(p.all_surface_forms())
                raw_asr_snippets: list[str] = []
                if scoped_profile is not None:
                    raw_asr_snippets.extend(scoped_profile.recent_facts)
                raw_asr_snippets.extend(recent_snippets)
                masked_asr_snippets = list(
                    dict.fromkeys(
                        masked
                        for snippet in raw_asr_snippets
                        if (masked := mask_hard_pii(snippet).masked_text.strip())
                    )
                )[:12]
                active_person_name_safe = (
                    mask_hard_pii(scoped_profile.display_name).masked_text.strip() or None
                    if scoped_profile is not None
                    else None
                )
                trans_payload = TranscribeInput(
                    audio_bytes=audio_bytes,
                    mime_type=audio_mime,
                    known_person_names=[
                        mask_hard_pii(name).masked_text for name in known_names[:60]
                    ],
                    active_person_name=active_person_name_safe,
                    recent_context_snippets=masked_asr_snippets,
                )
                trans_out = await gateway.transcribe(
                    scope=scope,
                    payload=trans_payload,
                )
                effective_text = (
                    trans_out.corrected_transcript.strip()
                    or trans_out.raw_transcript.strip()
                    or raw_text.strip()
                )
                if effective_text:
                    effective_text = apply_spoken_self_correction(effective_text)
                duration_ms = trans_out.duration_ms if trans_out.duration_ms is not None else 4000

            elif source is CaptureSource.IMAGE:
                try:
                    if gateway is None:
                        raise RuntimeError("AI ModelGatewayPort is not configured")
                    if in_memory_ephemeral_image is not None:
                        img_bytes, img_mime = in_memory_ephemeral_image
                    elif ephemeral_image_key is not None:
                        img_bytes, img_mime = await self.container.storage.get_decrypted(
                            user_id=scope.user_id,
                            dek=dek,
                            object_key=ephemeral_image_key,
                        )
                    elif image_object_key is not None:
                        img_bytes, img_mime = await self.container.storage.get_decrypted(
                            user_id=scope.user_id,
                            dek=dek,
                            object_key=image_object_key,
                        )
                    else:
                        raise RuntimeError("No screenshot image bytes available")

                    parsed_shot = await gateway.parse_screenshot(
                        scope=scope,
                        image_bytes=img_bytes,
                        mime_type=img_mime,
                        target_person_name=scoped_profile.display_name if scoped_profile else None,
                    )
                    screenshot_is_group = parsed_shot.is_group_chat
                    formatted = format_screenshot_bubbles(
                        parsed_shot.bubbles,
                        default_target_label=(
                            scoped_profile.display_name if scoped_profile else "对方"
                        ),
                    )
                    shot_text = formatted or parsed_shot.formatted_transcript.strip()
                    if raw_text.strip() and shot_text:
                        effective_text = f"{shot_text}\n{raw_text.strip()}"
                    else:
                        effective_text = shot_text or raw_text.strip()
                finally:
                    # PIA Shield 2: IMMEDIATELY destroy ephemeral screenshot bytes after bubble parsing!
                    in_memory_ephemeral_image = None
                    if ephemeral_image_key is not None:
                        await self.container.storage.delete_object(object_key=ephemeral_image_key)

            # 2b. Independent Safety Channel (Invariant I5)
            if safety_svc is not None and effective_text.strip():
                try:
                    safety_decision = await safety_svc.evaluate(
                        scope=scope,
                        text=effective_text,
                    )
                except Exception:
                    logger.warning("safety_evaluate_failed", capture_id=str(capture_id))

            # 2c. Structured Record & Fact Extraction (with pre-LLM hard PII masking!)
            if gateway is None:
                raise RuntimeError("AI ModelGatewayPort is not configured")
            if not effective_text.strip():
                raise ValueError("Empty transcript after modality ingestion")

            masked_for_llm = mask_hard_pii(effective_text).masked_text
            now_utc = self.container.clock.now()
            local_today = now_utc.astimezone(ZoneInfo(tz_name)).date()

            known_person_refs: list[KnownPersonRef] = []
            for p in known_profiles:
                masked_recent_facts = [
                    masked
                    for fact in p.recent_facts
                    if (masked := mask_hard_pii(fact).masked_text.strip())
                ]
                known_person_refs.append(
                    KnownPersonRef(
                        person_id=p.person_id,
                        display_name=p.display_name,
                        aliases=list(p.aliases),
                        tier=p.tier,
                        role_tag=p.role_tag,
                        recent_facts=masked_recent_facts,
                    )
                )

            ordered_profiles = (
                [
                    scoped_profile,
                    *(p for p in known_profiles if p.person_id != scoped_profile.person_id),
                ]
                if scoped_profile is not None
                else list(known_profiles)
            )
            recent_history_hints: list[str] = []
            seen_hints: set[str] = set()
            for p in ordered_profiles:
                safe_name = mask_hard_pii(p.display_name).masked_text.strip()
                for fact in p.recent_facts:
                    safe_fact = mask_hard_pii(fact).masked_text.strip()
                    if not safe_fact:
                        continue
                    hint_line = f"{safe_name}：{safe_fact}" if safe_name else safe_fact
                    if hint_line not in seen_hints:
                        seen_hints.add(hint_line)
                        recent_history_hints.append(hint_line)
                        if len(recent_history_hints) >= 12:
                            break
                if len(recent_history_hints) >= 12:
                    break

            extract_in = ExtractRecordInput(
                text=masked_for_llm,
                current_date=local_today.isoformat(),
                route_mode=route_mode.value,
                scoped_person_id=(scoped_person_id if route_mode is RouteMode.CURRENT else None),
                scoped_person_tier=(
                    scoped_profile.tier
                    if (route_mode is RouteMode.CURRENT and scoped_profile is not None)
                    else None
                ),
                input_source=source.value,
                known_persons=known_person_refs,
                recent_history_hints=recent_history_hints,
            )
            extract_out, prompt_version, model_profile = await gateway.extract_record(
                scope=scope,
                payload=extract_in,
            )

            return await self._persist_extracted_capture(
                scope=scope,
                capture_id=capture_id,
                source=source,
                route_mode=route_mode,
                scoped_person_id=scoped_person_id,
                effective_text=effective_text,
                duration_ms=duration_ms,
                screenshot_is_group=screenshot_is_group,
                safety_decision=safety_decision,
                extract_out=extract_out,
                prompt_version=prompt_version,
                model_profile=model_profile,
                idem_row_id=idem_row_id,
                idem_status_code=idem_status_code,
                response_serializer=response_serializer,
                facts_by_person=facts_by_person,
            )

        except Exception as exc:
            logger.warning(
                "capture_pipeline_fallback_to_note",
                capture_id=str(capture_id),
                exc_type=type(exc).__name__,
            )
            return await self._persist_fallback_note(
                scope=scope,
                capture_id=capture_id,
                source=source,
                route_mode=route_mode,
                scoped_person_id=scoped_person_id,
                fallback_text=effective_text or raw_text,
                duration_ms=duration_ms,
                safety_decision=safety_decision,
                idem_row_id=idem_row_id,
                idem_status_code=idem_status_code,
                response_serializer=response_serializer,
            )

    # =========================================================================
    # Transaction 2a: Normal Extracted Persistence
    # =========================================================================

    async def _persist_extracted_capture(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        source: CaptureSource,
        route_mode: RouteMode,
        scoped_person_id: UUID | None,
        effective_text: str,
        duration_ms: int | None,
        screenshot_is_group: bool,
        safety_decision: SafetyDecision,
        extract_out: Any,
        prompt_version: str,
        model_profile: str,
        idem_row_id: UUID | None,
        idem_status_code: int,
        response_serializer: Callable[[CaptureResponse], dict[str, Any]] | None = None,
        facts_by_person: dict[UUID, list[str]] | None = None,
    ) -> CaptureResponse:
        gate_cfg = self._gate_config()

        async with self.container.db.user_session(scope) as session:
            capture = await session.scalar(
                select(CaptureRow)
                .where(CaptureRow.id == capture_id, CaptureRow.user_id == scope.user_id)
                .with_for_update()
            )
            if capture is None:
                raise NotFoundError("Capture not found")

            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)
            known_profiles, person_rows_by_id = await load_known_person_profiles(
                session,
                scope=scope,
                dek=dek,
                cipher=self.container.cipher,
            )
            if facts_by_person is None:
                facts_by_person, _ = await load_recent_person_context(
                    session,
                    scope,
                    dek,
                    self.container.cipher,
                    [p.person_id for p in known_profiles],
                )
            known_profiles = [
                replace(p, recent_facts=tuple(facts_by_person.get(p.person_id, ())))
                for p in known_profiles
            ]
            profiles_by_id: dict[UUID, KnownPersonProfile] = {
                p.person_id: p for p in known_profiles
            }

            raw_facts: list[ExtractedFactItem] = list(extract_out.facts)
            # Apply deterministic right-bubble filter on screenshots or bubble-tagged inputs
            if source is CaptureSource.IMAGE or "[RIGHT_BUBBLE/" in effective_text:
                raw_facts = filter_screenshot_facts(raw_facts)

            # In group chat screenshots, speakers without explicit target verification become UNKNOWN
            normalized_facts: list[ExtractedFactItem] = []
            for f in raw_facts:
                if screenshot_is_group and f.source_speaker is SourceSpeaker.NARRATOR:
                    normalized_facts.append(
                        f.model_copy(update={"source_speaker": SourceSpeaker.UNKNOWN})
                    )
                else:
                    normalized_facts.append(f)

            candidate_hints = [
                CandidateHint(
                    mention=p.mention,
                    matched_person_id=p.matched_person_id,
                    candidate_person_ids=tuple(p.candidate_person_ids),
                    is_new_person=p.is_new_person,
                    confidence=p.confidence,
                )
                for p in extract_out.persons
            ]

            # Resolve every fact's person target deterministically
            resolved_pairs = [
                (
                    f,
                    resolve_mention_for_fact(
                        mention=f.person_mention,
                        fact_person_id_hint=f.person_id,
                        fact_content=f.content,
                        full_transcript=effective_text,
                        route_mode=route_mode,
                        scoped_person_id=scoped_person_id,
                        known_people=known_profiles,
                        candidate_hints=candidate_hints,
                    ),
                )
                for f in normalized_facts
            ]

            distinct_targets: set[str] = set()
            for _, res in resolved_pairs:
                if res.matched_person_id is not None:
                    distinct_targets.add(str(res.matched_person_id))
                elif res.is_new_person and res.mention:
                    distinct_targets.add(f"new:{normalize_mention(res.mention)}")
            utterance_multi_person = len(distinct_targets) >= 2

            # Provision any newly discovered crystal provisional persons (deduped by normalized name)
            new_persons_by_norm: dict[str, KnownPersonProfile] = {}
            newly_created_person_ids: set[UUID] = set()
            for _, res in resolved_pairs:
                if res.is_new_person:
                    norm_name = normalize_mention(res.mention) or "未命名角色"
                    if norm_name not in new_persons_by_norm:
                        p_row = await create_person_with_primary_alias(
                            session,
                            scope=scope,
                            dek=dek,
                            cipher=self.container.cipher,
                            display_name=res.mention or "未命名角色",
                            tier=Tier.CRYSTAL,
                            state=PersonState.PROVISIONAL,
                            now=now,
                        )
                        person_rows_by_id[p_row.id] = p_row
                        profile = KnownPersonProfile(
                            person_id=p_row.id,
                            display_name=res.mention.strip() or "未命名角色",
                            aliases=(res.mention.strip(),) if res.mention.strip() else (),
                            tier=Tier.CRYSTAL,
                            state=PersonState.PROVISIONAL,
                            role_tag="",
                            monogram=p_row.monogram,
                        )
                        profiles_by_id[p_row.id] = profile
                        new_persons_by_norm[norm_name] = profile
                        newly_created_person_ids.add(p_row.id)

            # Also handle global note with 0 extracted facts where suggest_new_person or 1 person was mentioned
            if not resolved_pairs:
                if scoped_person_id is not None and scoped_person_id in profiles_by_id:
                    pass  # handled below as pure note slip on scoped_person_id
                elif extract_out.suggest_new_person and route_mode is RouteMode.GLOBAL:
                    s_name = str(extract_out.suggest_new_person).strip()
                    if s_name and not is_non_person_stopword_mention(s_name):
                        p_row = await create_person_with_primary_alias(
                            session,
                            scope=scope,
                            dek=dek,
                            cipher=self.container.cipher,
                            display_name=s_name,
                            tier=Tier.CRYSTAL,
                            state=PersonState.PROVISIONAL,
                            now=now,
                        )
                        profile = KnownPersonProfile(
                            person_id=p_row.id,
                            display_name=s_name,
                            aliases=(s_name,),
                            tier=Tier.CRYSTAL,
                            state=PersonState.PROVISIONAL,
                            monogram=p_row.monogram,
                        )
                        profiles_by_id[p_row.id] = profile
                        newly_created_person_ids.add(p_row.id)

            # Group facts into:
            # (A) Resolved person groups: OrderedDict[UUID, list[tuple[ExtractedFactItem, MentionResolution]]]
            # (B) Ambiguous groups: OrderedDict[tuple[str, tuple[UUID, ...]], list[ExtractedFactItem]]
            person_groups: OrderedDict[UUID, list[tuple[ExtractedFactItem, Any]]] = OrderedDict()
            ambiguous_groups: OrderedDict[tuple[str, tuple[UUID, ...]], list[ExtractedFactItem]] = (
                OrderedDict()
            )

            for f_item, res in resolved_pairs:
                if res.is_new_person:
                    norm_name = normalize_mention(res.mention) or "未命名角色"
                    new_prof = new_persons_by_norm[norm_name]
                    person_groups.setdefault(new_prof.person_id, []).append((f_item, res))
                elif res.matched_person_id is not None:
                    person_groups.setdefault(res.matched_person_id, []).append((f_item, res))
                else:
                    amb_key = (res.mention or "", res.candidate_person_ids)
                    ambiguous_groups.setdefault(amb_key, []).append(f_item)

            # Ensure scoped_person gets a slip even when 0 facts were extracted for them
            if (
                scoped_person_id is not None
                and scoped_person_id in profiles_by_id
                and scoped_person_id not in person_groups
                and not ambiguous_groups
            ):
                person_groups[scoped_person_id] = []
            elif not person_groups and not ambiguous_groups and newly_created_person_ids:
                for pid in newly_created_person_ids:
                    person_groups[pid] = []
            elif not person_groups and not ambiguous_groups:
                # Global casual note without structured facts: match single mentioned known person if any
                hit_known = [
                    p
                    for p in known_profiles
                    if any(s in effective_text for s in p.all_surface_forms() if s)
                ]
                if len(hit_known) == 1:
                    person_groups[hit_known[0].person_id] = []
                elif (
                    not hit_known
                    and route_mode is RouteMode.GLOBAL
                    and effective_text.strip()
                    and (hist_res := resolve_by_history_clues(effective_text, known_profiles))
                    is not None
                ):
                    if (
                        hist_res.matched_person_id is not None
                        and hist_res.clause_person_unambiguous
                    ):
                        person_groups[hist_res.matched_person_id] = []
                    else:
                        ambiguous_groups[("", hist_res.candidate_person_ids)] = []
                elif effective_text.strip() or capture.audio_object_key or capture.image_object_key:
                    cand_tuple = tuple(p.person_id for p in (hit_known or known_profiles))
                    ambiguous_groups[("", cand_tuple)] = []

            record_kind = self._determine_record_kind(source, capture)
            media_key = capture.audio_object_key or capture.image_object_key

            splits: list[CaptureSplitDto] = []
            inbox_item_ids: list[UUID] = []
            pending_fact_count = 0
            matched_highlight_names: set[str] = set()

            # -----------------------------------------------------------------
            # Persist per-person (+ per-event-group) RecordRow + FactRow + InboxItemRow + ReminderRow
            # -----------------------------------------------------------------
            for pid, fact_entries in person_groups.items():
                p_prof = profiles_by_id[pid]
                matched_highlight_names.add(p_prof.display_name)
                for surf in p_prof.all_surface_forms():
                    if surf and surf in effective_text:
                        matched_highlight_names.add(surf)

                # Evaluate GatePolicy for every fact of this person first so we know RecordReviewState
                evaluated_facts: list[tuple[ExtractedFactItem, Any, bool, Any]] = []
                for f_item, res in fact_entries:
                    matched_highlight_names.update(
                        [f_item.person_mention]
                        if f_item.person_mention and f_item.person_mention in effective_text
                        else []
                    )
                    pol_flip = False
                    if p_prof.state is PersonState.ACTIVE:
                        pol_flip = await check_polarity_flip_for_person(
                            session,
                            scope=scope,
                            person_id=pid,
                            fact_type=f_item.fact_type,
                            polarity=f_item.polarity,
                            content=f_item.content,
                            dek=dek,
                            cipher=self.container.cipher,
                        )

                    if res.is_cross_person_in_scoped_mode:
                        # Cross-person mention inside `route_mode == "current"` MUST go pending!
                        gate_in = GateInput(
                            fact_type=f_item.fact_type,
                            confidence=f_item.confidence,
                            candidate_count=1,
                            multi_person=True,
                            scoped_locked=False,
                            clause_person_unambiguous=False,
                            is_venting=bool(extract_out.is_venting_or_irony),
                            tier=p_prof.tier,
                            person_state=p_prof.state,
                            source_speaker=f_item.source_speaker,
                            polarity_flipped=pol_flip,
                            extraction_ok=True,
                        )
                    else:
                        gate_in = GateInput(
                            fact_type=f_item.fact_type,
                            confidence=f_item.confidence,
                            candidate_count=1,
                            multi_person=utterance_multi_person
                            or (not res.clause_person_unambiguous),
                            scoped_locked=res.scoped_locked,
                            clause_person_unambiguous=res.clause_person_unambiguous,
                            is_venting=bool(extract_out.is_venting_or_irony),
                            tier=p_prof.tier,
                            person_state=p_prof.state,
                            source_speaker=f_item.source_speaker,
                            polarity_flipped=pol_flip,
                            extraction_ok=True,
                        )
                    decision = decide_gate(gate_in, gate_cfg, now=now)
                    evaluated_facts.append((f_item, decision, pol_flip, res))

                # Sub-group this person's facts into separate slips when 2+ distinct event_groups exist
                event_fact_groups = partition_by_event_group(
                    evaluated_facts,
                    lambda entry: entry[0].event_group,
                )

                for group_facts in event_fact_groups:
                    group_is_cross_in_current = bool(
                        group_facts
                        and all(r.is_cross_person_in_scoped_mode for _, _, _, r in group_facts)
                    )

                    # Determine RecordReviewState strictly from this slip's facts
                    if group_is_cross_in_current:
                        rec_review_state = RecordReviewState.HELD
                    elif group_facts and all(
                        dec.record_review_state is RecordReviewState.HELD
                        for _, dec, _, _ in group_facts
                    ):
                        rec_review_state = RecordReviewState.HELD
                    elif (
                        not group_facts
                        and bool(extract_out.is_venting_or_irony)
                        and p_prof.tier is Tier.GOLD
                    ):
                        rec_review_state = RecordReviewState.HELD
                    else:
                        rec_review_state = RecordReviewState.NORMAL

                    # Build natural slip title & body (zero cross-person or cross-event text bleed)
                    raw_contents = list(
                        dict.fromkeys(
                            clean
                            for f_item, _, _, _ in group_facts
                            if (clean := strip_forbidden_dev_jargon(f_item.content).strip())
                        )
                    )
                    if raw_contents:
                        slip_body = "；".join(raw_contents)
                    elif len(person_groups) == 1:
                        slip_body = strip_forbidden_dev_jargon(effective_text)
                    else:
                        slip_body = p_prof.display_name or "随手手札"

                    first_title = next(
                        (
                            strip_forbidden_dev_jargon(f_item.title)
                            for f_item, _, _, _ in group_facts
                            if f_item.title.strip()
                        ),
                        "",
                    )
                    slip_tag = build_natural_slip_tag(first_title, slip_body)

                    rec_id = new_id()
                    record_row = RecordRow(
                        id=rec_id,
                        user_id=scope.user_id,
                        person_id=pid,
                        capture_id=capture.id,
                        kind=record_kind.value,
                        title_enc=self.container.cipher.encrypt_str(
                            dek,
                            slip_tag,
                            user_id=scope.user_id,
                            table="records",
                            column="title_enc",
                            row_id=rec_id,
                        ),
                        body_enc=self.container.cipher.encrypt_str(
                            dek,
                            slip_body,
                            user_id=scope.user_id,
                            table="records",
                            column="body_enc",
                            row_id=rec_id,
                        ),
                        snippet_enc=self.container.cipher.encrypt_str(
                            dek,
                            slip_body[:60],
                            user_id=scope.user_id,
                            table="records",
                            column="snippet_enc",
                            row_id=rec_id,
                        ),
                        media_object_key=media_key,
                        audio_duration_ms=duration_ms if record_kind is RecordKind.VOICE else None,
                        happened_on=now.astimezone(ZoneInfo(tz_name)).date(),
                        pinned=False,
                        review_state=rec_review_state.value,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record_row)
                    await session.flush()

                    pending_by_inbox_kind: dict[InboxItemKind, list[FactRow]] = defaultdict(list)
                    slip_fact_rows: list[FactRow] = []

                    for f_item, decision, _, _ in group_facts:
                        clean_fact_content = strip_forbidden_dev_jargon(f_item.content)
                        if not clean_fact_content:
                            continue
                        fact_id = new_id()
                        parsed_tdate = _safe_parse_iso_date(f_item.trigger_date)
                        fact_row = FactRow(
                            id=fact_id,
                            user_id=scope.user_id,
                            person_id=pid,
                            record_id=rec_id,
                            capture_id=capture.id,
                            type=f_item.fact_type.value,
                            polarity=f_item.polarity.value,
                            time_status=f_item.time_status.value,
                            source_speaker=f_item.source_speaker.value,
                            content_enc=self.container.cipher.encrypt_str(
                                dek,
                                clean_fact_content,
                                user_id=scope.user_id,
                                table="facts",
                                column="content_enc",
                                row_id=fact_id,
                            ),
                            trigger_date=parsed_tdate,
                            is_lunar=f_item.is_lunar,
                            lunar_month=f_item.lunar_month,
                            lunar_day=f_item.lunar_day,
                            lunar_is_leap_month=False,
                            status=decision.status.value,
                            pending_reason=(
                                decision.pending_reason.value if decision.pending_reason else None
                            ),
                            needs_glance=decision.needs_glance,
                            confidence=f_item.confidence,
                            created_by=CreatedBy.AI.value,
                            valid_at=now,
                            prompt_version=prompt_version,
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(fact_row)
                        await session.flush()
                        slip_fact_rows.append(fact_row)

                        if decision.is_confirmed:
                            await apply_bitemporal_supersession_for_fact(
                                session,
                                scope=scope,
                                dek=dek,
                                cipher=self.container.cipher,
                                new_fact=fact_row,
                                new_fact_content=clean_fact_content,
                                now=now,
                            )
                        else:
                            pending_fact_count += 1
                            ikind = decision.inbox_kind or InboxItemKind.FACT_CONFIRM
                            pending_by_inbox_kind[ikind].append(fact_row)

                        # Proposed reminder when date/promise has trigger_date or lunar date
                        if f_item.fact_type in (FactType.DATE, FactType.PROMISE):
                            await upsert_reminder_for_fact(
                                session,
                                scope=scope,
                                dek=dek,
                                cipher=self.container.cipher,
                                fact=fact_row,
                                fact_content=clean_fact_content,
                                desired_status=(
                                    ReminderStatus.ACTIVE
                                    if decision.is_confirmed
                                    else ReminderStatus.PROPOSED
                                ),
                                now=now,
                                tz_name=tz_name,
                            )

                    # Create InboxItemRow(s) for pending facts (or new provisional person with 0 facts / venting note)
                    if (
                        pid in newly_created_person_ids
                        and InboxItemKind.NEW_PERSON_CONFIRM not in pending_by_inbox_kind
                    ):
                        pending_by_inbox_kind[InboxItemKind.NEW_PERSON_CONFIRM] = []
                    elif (
                        not group_facts
                        and rec_review_state is RecordReviewState.HELD
                        and p_prof.tier is Tier.GOLD
                    ):
                        pending_by_inbox_kind[InboxItemKind.VENTING_HOLD] = []

                    slip_inbox_ids: dict[InboxItemKind, UUID] = {}
                    for ikind, p_facts in pending_by_inbox_kind.items():
                        inbox_id = new_id()
                        expires_at = (
                            now + timedelta(hours=self.container.settings.venting_hold_expiry_hours)
                            if ikind is InboxItemKind.VENTING_HOLD
                            else None
                        )
                        # D1: "probably right, just unsure" drafts turn to ink on their own after
                        # 48h. Venting and person questions never auto-promote.
                        auto_ink_at = (
                            auto_ink_due_at(now)
                            if ikind is InboxItemKind.FACT_CONFIRM
                            and is_auto_ink_eligible(
                                _pending_reason_or_none(f.pending_reason) for f in p_facts
                            )
                            else None
                        )
                        payload_obj = {
                            "summary": slip_body[:160],
                            "mention": p_prof.display_name,
                        }
                        inbox_row = InboxItemRow(
                            id=inbox_id,
                            user_id=scope.user_id,
                            person_id=pid,
                            capture_id=capture.id,
                            record_id=rec_id,
                            kind=ikind.value,
                            status=InboxItemStatus.OPEN.value,
                            fact_ids=[f.id for f in p_facts],
                            candidate_person_ids=[pid],
                            payload_enc=self.container.cipher.encrypt_json(
                                dek,
                                payload_obj,
                                user_id=scope.user_id,
                                table="inbox_items",
                                column="payload_enc",
                                row_id=inbox_id,
                            ),
                            expires_at=expires_at,
                            auto_ink_at=auto_ink_at,
                            created_at=now,
                        )
                        session.add(inbox_row)
                        inbox_item_ids.append(inbox_id)
                        slip_inbox_ids[ikind] = inbox_id

                    if not group_is_cross_in_current:
                        pencil_state = derive_pencil_state(
                            review_state=rec_review_state,
                            open_inbox_kinds=tuple(slip_inbox_ids),
                            facts=[_fact_row_to_pencil_view(f) for f in slip_fact_rows],
                            is_new_person=pid in newly_created_person_ids,
                        )
                        splits.append(
                            CaptureSplitDto(
                                person_id=pid,
                                person_name=p_prof.display_name,
                                tier=p_prof.tier,
                                is_new_person=pid in newly_created_person_ids,
                                material_id=rec_id,
                                tag=slip_tag,
                                dur=format_duration_mmss(duration_ms),
                                body=slip_body,
                                presentation=pencil_state.presentation,
                                glance_hint=pencil_state.glance_hint,
                                fact_ids=[f.id for f in slip_fact_rows],
                                inbox_item_id=_primary_inbox_id(slip_inbox_ids),
                            )
                        )

            # -----------------------------------------------------------------
            # Persist Ambiguous Person Disambiguation Groups (held until resolved!)
            # -----------------------------------------------------------------
            for (amb_mention, cand_ids), amb_facts in ambiguous_groups.items():
                if amb_mention and amb_mention in effective_text:
                    matched_highlight_names.add(amb_mention)

                amb_previews = [
                    strip_forbidden_dev_jargon(f_item.content)
                    for f_item in amb_facts
                    if strip_forbidden_dev_jargon(f_item.content)
                ]
                amb_body = (
                    "；".join(amb_previews)
                    if amb_previews
                    else strip_forbidden_dev_jargon(effective_text) or "待确认手札"
                )
                first_amb_title = next(
                    (
                        strip_forbidden_dev_jargon(f_item.title)
                        for f_item in amb_facts
                        if f_item.title.strip()
                    ),
                    "",
                )
                amb_tag = build_natural_slip_tag(first_amb_title, amb_body)

                if cand_ids:
                    holder_person_id = cand_ids[0]
                else:
                    placeholder = await get_or_create_unassigned_placeholder(
                        session,
                        scope=scope,
                        dek=dek,
                        cipher=self.container.cipher,
                        now=now,
                    )
                    holder_person_id = placeholder.id

                amb_rec_id = new_id()
                amb_rec_row = RecordRow(
                    id=amb_rec_id,
                    user_id=scope.user_id,
                    person_id=holder_person_id,
                    capture_id=capture.id,
                    kind=record_kind.value,
                    title_enc=self.container.cipher.encrypt_str(
                        dek,
                        amb_tag,
                        user_id=scope.user_id,
                        table="records",
                        column="title_enc",
                        row_id=amb_rec_id,
                    ),
                    body_enc=self.container.cipher.encrypt_str(
                        dek,
                        amb_body,
                        user_id=scope.user_id,
                        table="records",
                        column="body_enc",
                        row_id=amb_rec_id,
                    ),
                    snippet_enc=self.container.cipher.encrypt_str(
                        dek,
                        amb_body[:60],
                        user_id=scope.user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=amb_rec_id,
                    ),
                    media_object_key=media_key,
                    audio_duration_ms=duration_ms if record_kind is RecordKind.VOICE else None,
                    happened_on=now.astimezone(ZoneInfo(tz_name)).date(),
                    pinned=False,
                    review_state=RecordReviewState.HELD.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(amb_rec_row)
                await session.flush()

                created_amb_fact_ids: list[UUID] = []
                for f_item in amb_facts:
                    clean_c = strip_forbidden_dev_jargon(f_item.content)
                    if not clean_c:
                        continue
                    fid = new_id()
                    f_row = FactRow(
                        id=fid,
                        user_id=scope.user_id,
                        person_id=None,
                        record_id=amb_rec_id,
                        capture_id=capture.id,
                        type=f_item.fact_type.value,
                        polarity=f_item.polarity.value,
                        time_status=f_item.time_status.value,
                        source_speaker=f_item.source_speaker.value,
                        content_enc=self.container.cipher.encrypt_str(
                            dek,
                            clean_c,
                            user_id=scope.user_id,
                            table="facts",
                            column="content_enc",
                            row_id=fid,
                        ),
                        trigger_date=_safe_parse_iso_date(f_item.trigger_date),
                        is_lunar=f_item.is_lunar,
                        lunar_month=f_item.lunar_month,
                        lunar_day=f_item.lunar_day,
                        lunar_is_leap_month=False,
                        status=FactStatus.PENDING.value,
                        pending_reason=PendingReason.AMBIGUOUS_PERSON.value,
                        confidence=f_item.confidence,
                        created_by=CreatedBy.AI.value,
                        valid_at=now,
                        prompt_version=prompt_version,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(f_row)
                    created_amb_fact_ids.append(fid)
                    pending_fact_count += 1

                inbox_id = new_id()
                amb_payload_obj: dict[str, Any] = {
                    "summary": amb_body[:160],
                    "mention": amb_mention or None,
                }
                amb_inbox_kind = (
                    InboxItemKind.PERSON_DISAMBIGUATION
                    if (cand_ids or known_profiles)
                    else InboxItemKind.FACT_CONFIRM
                )
                session.add(
                    InboxItemRow(
                        id=inbox_id,
                        user_id=scope.user_id,
                        person_id=None,
                        capture_id=capture.id,
                        record_id=amb_rec_id,
                        kind=amb_inbox_kind.value,
                        status=InboxItemStatus.OPEN.value,
                        fact_ids=created_amb_fact_ids,
                        candidate_person_ids=list(cand_ids),
                        payload_enc=self.container.cipher.encrypt_json(
                            dek,
                            amb_payload_obj,
                            user_id=scope.user_id,
                            table="inbox_items",
                            column="payload_enc",
                            row_id=inbox_id,
                        ),
                        created_at=now,
                    )
                )
                inbox_item_ids.append(inbox_id)

            await session.flush()

            # Build XSS-safe transcriptHtml
            transcript_html = build_transcript_html(effective_text, matched_highlight_names)
            total_pending = max(pending_fact_count, len(inbox_item_ids))
            final_status = (
                CaptureStatus.NEEDS_REVIEW if total_pending > 0 else CaptureStatus.COMPLETED
            )
            undo_deadline = now + timedelta(seconds=gate_cfg.undo_window_seconds)
            crisis_dto = (
                CrisisCardDto.from_contract(safety_decision.crisis_card)
                if safety_decision.crisis_card is not None
                else None
            )

            summary_blob = {
                "splits": [s.model_dump(mode="json", by_alias=True) for s in splits],
                "pendingCount": total_pending,
                "inboxItemIds": [str(iid) for iid in inbox_item_ids],
                "crisisCard": (
                    crisis_dto.model_dump(mode="json", by_alias=True)
                    if crisis_dto is not None
                    else None
                ),
            }

            capture.status = final_status.value
            capture.duration_ms = duration_ms
            capture.safety_category = safety_decision.category.value
            capture.prompt_version = prompt_version
            capture.model_profile = model_profile
            capture.undo_deadline = undo_deadline
            capture.completed_at = now
            capture.transcript_enc = self.container.cipher.encrypt_str(
                dek,
                effective_text,
                user_id=scope.user_id,
                table="captures",
                column="transcript_enc",
                row_id=capture.id,
            )
            capture.transcript_marked_enc = self.container.cipher.encrypt_str(
                dek,
                transcript_html,
                user_id=scope.user_id,
                table="captures",
                column="transcript_marked_enc",
                row_id=capture.id,
            )
            capture.splits_summary_enc = self.container.cipher.encrypt_json(
                dek,
                summary_blob,
                user_id=scope.user_id,
                table="captures",
                column="splits_summary_enc",
                row_id=capture.id,
            )
            await session.flush()

            resp = CaptureResponse(
                capture_id=capture.id,
                source=source,
                route_mode=route_mode,
                scoped_person_id=scoped_person_id,
                status=final_status,
                transcript=effective_text,
                transcript_html=transcript_html,
                splits=splits,
                pending_count=total_pending,
                inbox_item_ids=inbox_item_ids,
                safety_category=safety_decision.category,
                crisis_card=crisis_dto,
                undo_deadline=undo_deadline,
                created_at=capture.created_at,
                completed_at=now,
            )
            if idem_row_id is not None:
                idem_row = await session.get(IdempotencyKeyRow, idem_row_id)
                if idem_row is not None:
                    body = (
                        response_serializer(resp)
                        if response_serializer is not None
                        else resp.model_dump(mode="json", by_alias=True)
                    )
                    await self.container.idempotency.complete(
                        session,
                        idem_row,
                        status_code=idem_status_code,
                        response_body=body,
                    )
            return resp

    # =========================================================================
    # Transaction 2b: Zero-Data-Loss Fallback (`saved_as_note`, Invariant I2)
    # =========================================================================

    async def _persist_fallback_note(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        source: CaptureSource,
        route_mode: RouteMode,
        scoped_person_id: UUID | None,
        fallback_text: str,
        duration_ms: int | None,
        safety_decision: SafetyDecision,
        idem_row_id: UUID | None,
        idem_status_code: int,
        response_serializer: Callable[[CaptureResponse], dict[str, Any]] | None = None,
    ) -> CaptureResponse:
        gate_cfg = self._gate_config()
        async with self.container.db.user_session(scope) as session:
            capture = await session.scalar(
                select(CaptureRow)
                .where(CaptureRow.id == capture_id, CaptureRow.user_id == scope.user_id)
                .with_for_update()
            )
            if capture is None:
                raise NotFoundError("Capture not found")

            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)
            known_profiles, _ = await load_known_person_profiles(
                session,
                scope=scope,
                dek=dek,
                cipher=self.container.cipher,
            )
            profiles_by_id = {p.person_id: p for p in known_profiles}

            resolved_person_id: UUID | None = None
            resolved_person_name = "待归档"
            resolved_tier = Tier.CRYSTAL

            if scoped_person_id is not None and scoped_person_id in profiles_by_id:
                resolved_person_id = scoped_person_id
                resolved_person_name = profiles_by_id[scoped_person_id].display_name
                resolved_tier = profiles_by_id[scoped_person_id].tier
            elif fallback_text.strip():
                hits = [
                    p
                    for p in known_profiles
                    if any(s in fallback_text for s in p.all_surface_forms() if s)
                ]
                if len(hits) == 1:
                    resolved_person_id = hits[0].person_id
                    resolved_person_name = hits[0].display_name
                    resolved_tier = hits[0].tier

            if resolved_person_id is not None:
                record_person_id = resolved_person_id
                record_review_state = RecordReviewState.NORMAL
                inbox_person_id: UUID | None = resolved_person_id
                inbox_kind = InboxItemKind.FACT_CONFIRM
                candidate_ids = [resolved_person_id]
            else:
                placeholder = await get_or_create_unassigned_placeholder(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    now=now,
                )
                record_person_id = placeholder.id
                record_review_state = RecordReviewState.HELD
                inbox_person_id = None
                inbox_kind = (
                    InboxItemKind.PERSON_DISAMBIGUATION
                    if known_profiles
                    else InboxItemKind.FACT_CONFIRM
                )
                candidate_ids = [p.person_id for p in known_profiles]

            record_kind = (
                RecordKind.VOICE
                if source is CaptureSource.VOICE
                else (RecordKind.PHOTO if capture.image_object_key else RecordKind.NOTE)
            )
            body_text = strip_forbidden_dev_jargon(fallback_text.strip()) or (
                "原声录音已妥善保存，稍后可补充归类。"
                if source is CaptureSource.VOICE
                else "随手内容已妥善保存。"
            )
            slip_tag = build_natural_slip_tag(
                "原声留存" if source is CaptureSource.VOICE else "随手备忘",
                body_text,
            )

            rec_id = new_id()
            rec_row = RecordRow(
                id=rec_id,
                user_id=scope.user_id,
                person_id=record_person_id,
                capture_id=capture.id,
                kind=record_kind.value,
                title_enc=self.container.cipher.encrypt_str(
                    dek,
                    slip_tag,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=rec_id,
                ),
                body_enc=self.container.cipher.encrypt_str(
                    dek,
                    body_text,
                    user_id=scope.user_id,
                    table="records",
                    column="body_enc",
                    row_id=rec_id,
                ),
                snippet_enc=self.container.cipher.encrypt_str(
                    dek,
                    body_text[:60],
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                media_object_key=capture.audio_object_key or capture.image_object_key,
                audio_duration_ms=duration_ms if record_kind is RecordKind.VOICE else None,
                happened_on=now.astimezone(ZoneInfo(tz_name)).date(),
                pinned=False,
                review_state=record_review_state.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(rec_row)
            await session.flush()

            fact_id = new_id()
            fact_row = FactRow(
                id=fact_id,
                user_id=scope.user_id,
                person_id=inbox_person_id,
                record_id=rec_id,
                capture_id=capture.id,
                type=FactType.NOTE.value,
                polarity="neutral",
                time_status="current",
                source_speaker=SourceSpeaker.NARRATOR.value,
                content_enc=self.container.cipher.encrypt_str(
                    dek,
                    body_text,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fact_id,
                ),
                status=FactStatus.PENDING.value,
                pending_reason=PendingReason.EXTRACTION_FAILED.value,
                confidence=0.0,
                created_by=CreatedBy.AI.value,
                valid_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(fact_row)
            await session.flush()

            inbox_id = new_id()
            session.add(
                InboxItemRow(
                    id=inbox_id,
                    user_id=scope.user_id,
                    person_id=inbox_person_id,
                    capture_id=capture.id,
                    record_id=rec_id,
                    kind=inbox_kind.value,
                    status=InboxItemStatus.OPEN.value,
                    fact_ids=[fact_id],
                    candidate_person_ids=candidate_ids,
                    payload_enc=self.container.cipher.encrypt_json(
                        dek,
                        {"summary": body_text[:160], "mention": None},
                        user_id=scope.user_id,
                        table="inbox_items",
                        column="payload_enc",
                        row_id=inbox_id,
                    ),
                    created_at=now,
                )
            )

            splits: list[CaptureSplitDto] = []
            if resolved_person_id is not None:
                splits.append(
                    CaptureSplitDto(
                        person_id=resolved_person_id,
                        person_name=resolved_person_name,
                        tier=resolved_tier,
                        is_new_person=False,
                        material_id=rec_id,
                        tag=slip_tag,
                        dur=format_duration_mmss(duration_ms),
                        body=body_text,
                    )
                )

            transcript_html = build_transcript_html(
                fallback_text,
                [resolved_person_name] if resolved_person_id is not None else [],
            )
            undo_deadline = now + timedelta(seconds=gate_cfg.undo_window_seconds)
            crisis_dto = (
                CrisisCardDto.from_contract(safety_decision.crisis_card)
                if safety_decision.crisis_card is not None
                else None
            )
            summary_blob = {
                "splits": [s.model_dump(mode="json", by_alias=True) for s in splits],
                "pendingCount": 1,
                "inboxItemIds": [str(inbox_id)],
                "crisisCard": (
                    crisis_dto.model_dump(mode="json", by_alias=True)
                    if crisis_dto is not None
                    else None
                ),
            }

            capture.status = CaptureStatus.SAVED_AS_NOTE.value
            capture.error_code = PendingReason.EXTRACTION_FAILED.value
            capture.duration_ms = duration_ms
            capture.safety_category = safety_decision.category.value
            capture.undo_deadline = undo_deadline
            capture.completed_at = now
            capture.transcript_enc = self.container.cipher.encrypt_str(
                dek,
                fallback_text,
                user_id=scope.user_id,
                table="captures",
                column="transcript_enc",
                row_id=capture.id,
            )
            capture.transcript_marked_enc = self.container.cipher.encrypt_str(
                dek,
                transcript_html,
                user_id=scope.user_id,
                table="captures",
                column="transcript_marked_enc",
                row_id=capture.id,
            )
            capture.splits_summary_enc = self.container.cipher.encrypt_json(
                dek,
                summary_blob,
                user_id=scope.user_id,
                table="captures",
                column="splits_summary_enc",
                row_id=capture.id,
            )
            await session.flush()

            resp = CaptureResponse(
                capture_id=capture.id,
                source=source,
                route_mode=route_mode,
                scoped_person_id=scoped_person_id,
                status=CaptureStatus.SAVED_AS_NOTE,
                transcript=fallback_text,
                transcript_html=transcript_html,
                splits=splits,
                pending_count=1,
                inbox_item_ids=[inbox_id],
                safety_category=safety_decision.category,
                crisis_card=crisis_dto,
                undo_deadline=undo_deadline,
                created_at=capture.created_at,
                completed_at=now,
            )
            if idem_row_id is not None:
                idem_row = await session.get(IdempotencyKeyRow, idem_row_id)
                if idem_row is not None:
                    body = (
                        response_serializer(resp)
                        if response_serializer is not None
                        else resp.model_dump(mode="json", by_alias=True)
                    )
                    await self.container.idempotency.complete(
                        session,
                        idem_row,
                        status_code=idem_status_code,
                        response_body=body,
                    )
            return resp

    # =========================================================================
    # GET /captures/{id}, SSE /captures/{id}/events, POST /captures/{id}:undo
    # =========================================================================

    async def get_capture(self, *, scope: UserScope, capture_id: UUID) -> CaptureResponse:
        async with self.container.db.user_session(scope) as session:
            capture = await session.scalar(
                select(CaptureRow).where(
                    CaptureRow.id == capture_id,
                    CaptureRow.user_id == scope.user_id,
                )
            )
            if capture is None:
                raise NotFoundError("Capture not found")
            dek = await self.container.load_user_dek(session, scope)
            return self._capture_row_to_response(dek, capture)

    async def stream_capture_events(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
    ) -> AsyncIterator[dict[str, str]]:
        """Yield SSE events (`transcript`, `splits`, optional `needs_review`, and `done`)."""
        snap = await self.get_capture(scope=scope, capture_id=capture_id)

        # If queued and a background worker is running concurrently, poll briefly;
        # if still `received` with no external worker claiming it, process inline so SSE completes deterministically.
        if snap.status.value not in _TERMINAL_CAPTURE_STATUSES:
            for _ in range(3):
                await asyncio.sleep(0.03)
                snap = await self.get_capture(scope=scope, capture_id=capture_id)
                if snap.status.value in _TERMINAL_CAPTURE_STATUSES:
                    break
            if snap.status.value not in _TERMINAL_CAPTURE_STATUSES:
                ephemeral_key: str | None = None
                for ext in ("png", "jpg", "webp"):
                    cand_key = f"{scope.user_id}/captures/{capture_id}/_ephemeral.{ext}"
                    try:
                        async with self.container.db.user_session(scope) as s:
                            dek = await self.container.load_user_dek(s, scope)
                        await self.container.storage.get_decrypted(
                            user_id=scope.user_id,
                            dek=dek,
                            object_key=cand_key,
                        )
                        ephemeral_key = cand_key
                        break
                    except NotFoundError:
                        continue
                snap = await self.process_capture_by_id(
                    scope=scope,
                    capture_id=capture_id,
                    ephemeral_image_key=ephemeral_key,
                )

        yield {
            "event": "transcript",
            "data": json.dumps(
                {
                    "captureId": str(snap.capture_id),
                    "transcript": snap.transcript,
                    "transcriptHtml": snap.transcript_html,
                },
                ensure_ascii=False,
            ),
        }

        yield {
            "event": "splits",
            "data": json.dumps(
                {
                    "captureId": str(snap.capture_id),
                    "splits": [s.model_dump(mode="json", by_alias=True) for s in snap.splits],
                },
                ensure_ascii=False,
            ),
        }

        if snap.pending_count > 0:
            yield {
                "event": "needs_review",
                "data": json.dumps(
                    {
                        "captureId": str(snap.capture_id),
                        "pendingCount": snap.pending_count,
                        "inboxItemIds": [str(i) for i in snap.inbox_item_ids],
                    },
                    ensure_ascii=False,
                ),
            }

        yield {
            "event": "done",
            "data": json.dumps(
                {
                    "captureId": str(snap.capture_id),
                    "status": snap.status.value,
                    "pendingCount": snap.pending_count,
                    "undoDeadline": (
                        snap.undo_deadline.isoformat() if snap.undo_deadline else None
                    ),
                    "crisisCard": (
                        snap.crisis_card.model_dump(mode="json", by_alias=True)
                        if snap.crisis_card is not None
                        else None
                    ),
                },
                ensure_ascii=False,
            ),
        }

    async def undo_capture(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        idem_key: str,
        route_path: str,
    ) -> tuple[CaptureUndoResponse | None, IdempotencyHit | None]:
        req_hash = hash_request_payload({"action": "undo", "captureId": str(capture_id)})
        async with self.container.db.user_session(scope) as session:
            idem_row, hit = await self.container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=route_path,
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            capture = await session.scalar(
                select(CaptureRow)
                .where(
                    CaptureRow.id == capture_id,
                    CaptureRow.user_id == scope.user_id,
                )
                .with_for_update()
            )
            if capture is None:
                raise NotFoundError("Capture not found")
            if capture.status == CaptureStatus.REVERTED.value:
                raise ConflictError("Capture has already been reverted")

            now = self.container.clock.now()
            if capture.undo_deadline is None or now > capture.undo_deadline:
                raise ConflictError(
                    "The 5-second undo window has expired.",
                    error_code="undo_window_expired",
                )

            rec_cnt, fact_cnt, rem_cnt = await revert_capture_graph(
                session,
                scope=scope,
                capture=capture,
                now=now,
            )
            resp = CaptureUndoResponse(
                capture_id=capture.id,
                status=CaptureStatus.REVERTED,
                reverted_record_count=rec_cnt,
                rejected_fact_count=fact_cnt,
                cancelled_reminder_count=rem_cnt,
            )
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=200,
                response_body=resp.model_dump(mode="json", by_alias=True),
            )
            return resp, None

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _determine_record_kind(source: CaptureSource, capture: CaptureRow) -> RecordKind:
        if source is CaptureSource.VOICE:
            return RecordKind.VOICE
        if source is CaptureSource.IMAGE and capture.image_object_key:
            return RecordKind.PHOTO
        return RecordKind.NOTE

    def _capture_row_to_response(self, dek: bytes, row: CaptureRow) -> CaptureResponse:
        transcript = (
            self.container.cipher.decrypt_str(
                dek,
                row.transcript_enc,
                user_id=row.user_id,
                table="captures",
                column="transcript_enc",
                row_id=row.id,
            )
            if row.transcript_enc is not None
            else ""
        )
        transcript_html = (
            self.container.cipher.decrypt_str(
                dek,
                row.transcript_marked_enc,
                user_id=row.user_id,
                table="captures",
                column="transcript_marked_enc",
                row_id=row.id,
            )
            if row.transcript_marked_enc is not None
            else ""
        )

        splits: list[CaptureSplitDto] = []
        pending_count = 0
        inbox_item_ids: list[UUID] = []
        crisis_card: CrisisCardDto | None = None

        if row.splits_summary_enc is not None:
            summary_raw = self.container.cipher.decrypt_json(
                dek,
                row.splits_summary_enc,
                user_id=row.user_id,
                table="captures",
                column="splits_summary_enc",
                row_id=row.id,
            )
            if isinstance(summary_raw, dict):
                raw_splits = summary_raw.get("splits") or []
                splits = [CaptureSplitDto.model_validate(item) for item in raw_splits]
                pending_count = int(summary_raw.get("pendingCount") or 0)
                inbox_item_ids = [UUID(str(x)) for x in (summary_raw.get("inboxItemIds") or [])]
                if summary_raw.get("crisisCard"):
                    crisis_card = CrisisCardDto.model_validate(summary_raw["crisisCard"])

        return CaptureResponse(
            capture_id=row.id,
            source=CaptureSource(row.source),
            route_mode=RouteMode(row.route_mode),
            scoped_person_id=row.scoped_person_id,
            status=CaptureStatus(row.status),
            transcript=transcript,
            transcript_html=transcript_html,
            splits=splits,
            pending_count=pending_count,
            inbox_item_ids=inbox_item_ids,
            safety_category=SafetyCategory(row.safety_category),
            crisis_card=crisis_card,
            undo_deadline=row.undo_deadline,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


async def handle_process_capture_job(
    container: AppContainer,
    payload: Any,
) -> CaptureResponse:
    """Worker entrypoint for `TASK_PROCESS_CAPTURE` (`capture.process`).

    Accepts either a `dict[str, Any]` payload or a worker `ClaimedJob` instance.
    """
    raw_user_id = payload.get("user_id") if hasattr(payload, "get") else None
    if raw_user_id is None and hasattr(payload, "user_id"):
        raw_user_id = payload.user_id
    user_id = UUID(str(raw_user_id))
    capture_id = UUID(str(payload["capture_id"]))
    ephemeral_key_raw = payload.get("ephemeral_image_key")
    ephemeral_key = str(ephemeral_key_raw) if ephemeral_key_raw else None
    svc = CapturePipelineService(container)
    return await svc.process_capture_by_id(
        scope=UserScope(user_id=user_id),
        capture_id=capture_id,
        ephemeral_image_key=ephemeral_key,
    )


def decode_optional_base64(value: str | None, *, field_name: str) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValidationFailedError(f"Invalid base64 encoding for {field_name}") from exc
