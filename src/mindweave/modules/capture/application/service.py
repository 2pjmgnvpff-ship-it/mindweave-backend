"""Multimodal Capture Pipeline Application Service (Invariants I1, I2, I3, I5, I6, I11, I12)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, tuple_
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.deterministic_extractor import extract_deterministically
from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.ai.schemas.contracts import (
    ExtractedFactItem,
    ExtractRecordInput,
    KnownPersonRef,
    SafetyDecision,
    TranscribeInput,
)
from mindweave.ai.validators.asr_postprocess import postprocess_asr_transcript
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import CursorPage, strip_forbidden_dev_jargon
from mindweave.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamAIError,
    ValidationFailedError,
)
from mindweave.core.idempotency import IdempotencyHit, hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    ChatCitationRow,
    FactRow,
    IdempotencyKeyRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
)
from mindweave.modules.capture.application.dto import (
    CaptureAddSlipRequest,
    CaptureAddSlipResponse,
    CaptureCandidatePersonDto,
    CaptureHistoryItemDto,
    CaptureHistoryPersonSummaryDto,
    CaptureHistorySplitDto,
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
    derive_monogram,
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
    build_created_cursor,
    check_polarity_flip_for_person,
    create_person_with_primary_alias,
    decrypt_person_display_name,
    decrypt_person_role_tag,
    get_user_timezone,
    parse_created_cursor,
    require_person_row,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
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
from mindweave.shared.scopes import PersonScope, UserScope
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
    client_transcript: str | None = None
    speech_engine: str | None = None
    audio_duration_ms: int | None = None
    audio_bytes: bytes | None = None
    image_bytes: bytes | None = None
    keep_original_media: bool = False
    sync: bool | None = None
    client_capture_id: UUID | None = None
    idem_key: str | None = None
    route_path: str = "/v1/captures"


@dataclass(frozen=True, slots=True)
class VoiceTranscribeResult:
    """Result of pure in-chat conversational ASR (zero DB capture/record/fact writes)."""

    transcript: str
    corrected: str
    raw_transcript: str = ""
    duration_ms: int | None = None


@dataclass(slots=True)
class CapturePipelineService:
    container: AppContainer

    def _gate_config(self) -> GateConfig:
        return GateConfig(
            confidence_threshold=self.container.settings.gate_confidence_threshold,
            undo_window_seconds=self.container.settings.undo_window_seconds,
        )

    def _get_gateway(self) -> ModelGatewayPort | None:
        gw = cast(
            ModelGatewayPort | None,
            self.container.extras.get("gateway")
            or self.container.extras.get("model_gateway")
            or self.container.extras.get("ai_gateway"),
        )
        if gw is not None:
            meter = getattr(gw, "meter", None)
            if meter is not None and hasattr(meter, "bind_db"):
                meter.bind_db(self.container.db)
        return gw

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

        # 0. Per-user burst rate limit BEFORE media work or AI invocation (BC5 Gate 5A)
        self.container.rate_limiter.check(
            f"capture:{cmd.scope.user_id}:{cmd.source.value}",
            capacity=10,
            refill_per_second=10.0 / 60.0,
        )

        # 1. Pre-flight media & payload validation
        audio_info = None
        sanitized_img: SanitizedImage | None = None
        initial_text = (cmd.client_transcript or "").strip() or (cmd.text or "").strip()

        if cmd.source is CaptureSource.VOICE:
            if not cmd.audio_bytes and not initial_text:
                raise ValidationFailedError(
                    "Voice capture requires non-empty audio file or clientTranscript"
                )
            if cmd.audio_bytes:
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
            if not initial_text:
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
        audio_sha256 = (
            hashlib.sha256(cmd.audio_bytes).hexdigest() if cmd.audio_bytes else ""
        )
        image_sha256 = (
            hashlib.sha256(cmd.image_bytes).hexdigest() if cmd.image_bytes else ""
        )
        req_hash = hash_request_payload(
            {
                "routePath": cmd.route_path,
                "source": cmd.source.value,
                "routeMode": cmd.route_mode.value,
                "activePersonId": str(cmd.active_person_id) if cmd.active_person_id else None,
                "keepOriginalMedia": cmd.keep_original_media,
                "text": cmd.text or "",
                "clientTranscript": cmd.client_transcript or "",
                "speechEngine": cmd.speech_engine or "",
                "audioDurationMs": cmd.audio_duration_ms,
                "audioSha256": audio_sha256,
                "imageSha256": image_sha256,
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
        replay_status = 200 if force_sync else 201

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

            existing_capture = await session.get(CaptureRow, capture_id)
            if existing_capture is not None:
                if existing_capture.user_id != cmd.scope.user_id:
                    raise ConflictError("Capture ID already exists")
                if existing_capture.status in _TERMINAL_CAPTURE_STATUSES:
                    dek = await self.container.load_user_dek(session, cmd.scope)
                    existing_resp = await self._hydrate_capture_response(
                        session,
                        scope=cmd.scope,
                        dek=dek,
                        row=existing_capture,
                    )
                    if idem_row_id is not None:
                        idem_row_loaded = await session.get(IdempotencyKeyRow, idem_row_id)
                        if idem_row_loaded is not None:
                            body = (
                                response_serializer(existing_resp)
                                if response_serializer is not None
                                else existing_resp.model_dump(mode="json", by_alias=True)
                            )
                            await self.container.idempotency.complete(
                                session,
                                idem_row_loaded,
                                status_code=replay_status,
                                response_body=body,
                            )
                    return existing_resp, None, replay_status
                # Still `received` (re-entrant resume after transient interruption):
                # proceed straight to inline processing if requested, else 202.
                if not run_inline:
                    queued_dto = CaptureResponse(
                        capture_id=existing_capture.id,
                        source=CaptureSource(existing_capture.source),
                        route_mode=RouteMode(existing_capture.route_mode),
                        scoped_person_id=existing_capture.scoped_person_id,
                        status=CaptureStatus.RECEIVED,
                        duration_ms=existing_capture.duration_ms,
                        created_at=existing_capture.created_at,
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
            else:
                if cmd.active_person_id is not None:
                    active_person = await require_person_row(
                        session, cmd.scope, cmd.active_person_id
                    )
                    if (
                        active_person.archived_at is not None
                        or active_person.state == PersonState.ARCHIVED.value
                    ):
                        raise ValidationFailedError("Cannot capture into an archived person")

                tz_name = await get_user_timezone(session, cmd.scope)
                if cmd.source is CaptureSource.VOICE:
                    await self.container.quotas.check_voice_quota(
                        session,
                        cmd.scope,
                        additional_ms=cmd.audio_duration_ms or 0,
                        tz_name=tz_name,
                    )
                else:
                    await self.container.quotas.check_capture_quota(
                        session,
                        cmd.scope,
                        source=cmd.source.value,
                        tz_name=tz_name,
                    )

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
                        initial_text,
                        user_id=cmd.scope.user_id,
                        table="captures",
                        column="raw_text_enc",
                        row_id=capture_id,
                    )
                    if initial_text
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
                    duration_ms=cmd.audio_duration_ms,
                    keep_original_media=keep_image if cmd.source is CaptureSource.IMAGE else True,
                    raw_text_enc=raw_text_enc,
                    safety_category=SafetyCategory.NORMAL.value,
                    idempotency_key=effective_idem_key,
                    created_at=now,
                )
                try:
                    async with session.begin_nested():
                        session.add(capture_row)
                        await session.flush()
                except IntegrityError as exc:
                    raise ConflictError("Capture ID already exists") from exc

                if not run_inline:
                    assert job_queue is not None
                    await job_queue.enqueue(
                        session,
                        task_name=TASK_PROCESS_CAPTURE,
                        payload={
                            "capture_id": str(capture_id),
                            "user_id": str(cmd.scope.user_id),
                            "ephemeral_image_key": ephemeral_image_key,
                            "speech_engine": cmd.speech_engine,
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
                        duration_ms=cmd.audio_duration_ms,
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
        try:
            result_dto = await self.process_capture_by_id(
                scope=cmd.scope,
                capture_id=capture_id,
                in_memory_ephemeral_image=inline_ephemeral_img,
                idem_row_id=idem_row_id,
                idem_status_code=replay_status,
                response_serializer=response_serializer,
                speech_engine=cmd.speech_engine,
            )
        except BaseException:
            if cmd.idem_key is not None:
                try:
                    async with self.container.db.user_session(cmd.scope) as err_session:
                        await self.container.idempotency.release(
                            err_session,
                            scope=cmd.scope,
                            idem_key=cmd.idem_key,
                            method="POST",
                            path=cmd.route_path,
                        )
                except Exception:
                    logger.warning(
                        "capture_idempotency_release_failed",
                        capture_id=str(capture_id),
                    )
            raise
        return result_dto, None, replay_status

    @staticmethod
    def _to_v10_voice_response(cap_resp: CaptureResponse) -> VoiceIngestResponse:
        return VoiceIngestResponse(
            job_id=cap_resp.capture_id,
            route_mode=cap_resp.route_mode,
            transcript=cap_resp.transcript,
            transcript_html=cap_resp.transcript_html,
            splits=cap_resp.splits,
            candidate_persons=cap_resp.candidate_persons,
            pending_count=cap_resp.pending_count,
            undo_deadline=cap_resp.undo_deadline,
            crisis_card=cap_resp.crisis_card,
            inbox_item_ids=cap_resp.inbox_item_ids,
            needs_cloud_refinement=cap_resp.needs_cloud_refinement,
            extraction_mode=cap_resp.extraction_mode,
            error_code=cap_resp.error_code,
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

    async def transcribe_only(
        self,
        *,
        scope: UserScope,
        audio_bytes: bytes,
        active_person_id: UUID | None = None,
    ) -> VoiceTranscribeResult:
        """Transcribe conversational voice audio with person-grounded ASR without creating records or facts.

        Enforces per-user token-bucket rate limiting, audio size & magic-byte validation,
        `UserScope` / `PersonScope` + `FORCE RLS`, active-person ownership verification,
        confirmed/non-held context loading, hard-PII masking, and `asr_postprocess.py`.
        """
        self.container.rate_limiter.check(
            f"voice_transcribe:{scope.user_id}",
            capacity=20,
            refill_per_second=0.5,
        )
        if not audio_bytes:
            raise ValidationFailedError("Voice transcription requires a non-empty audio file")
        try:
            audio_info = validate_and_sniff_audio(
                audio_bytes,
                max_bytes=self.container.settings.max_audio_bytes,
            )
        except MediaTooLargeError as exc:
            raise PayloadTooLargeError(str(exc)) from exc
        except InvalidMediaFormatError as exc:
            raise ValidationFailedError(str(exc)) from exc

        gateway = self._get_gateway()
        if gateway is None:
            raise UpstreamAIError("AI Model Gateway is not configured")

        # Load person context strictly under PersonScope (when scoped) or UserScope + FORCE RLS
        async with self.container.db.user_session(scope) as session:
            tz_name = await get_user_timezone(session, scope)
            await self.container.quotas.check_voice_quota(session, scope, tz_name=tz_name)
            dek = await self.container.load_user_dek(session, scope)

            scoped_profile: KnownPersonProfile | None = None
            known_names: list[str] = []
            raw_asr_snippets: list[str] = []

            if active_person_id is not None:
                person_scope = PersonScope(scope.user_id, active_person_id)
                person_row = await require_person_row(
                    session, person_scope.user_scope, person_scope.person_id
                )
                alias_rows = (
                    await session.scalars(
                        select(PersonAliasRow).where(
                            PersonAliasRow.user_id == person_scope.user_id,
                            PersonAliasRow.person_id == person_scope.person_id,
                        )
                    )
                ).all()
                aliases: list[str] = []
                for alias_row in alias_rows:
                    decrypted_alias = self.container.cipher.decrypt_str(
                        dek,
                        alias_row.alias_enc,
                        user_id=person_scope.user_id,
                        table="person_aliases",
                        column="alias_enc",
                        row_id=alias_row.id,
                    )
                    if decrypted_alias and decrypted_alias not in aliases:
                        aliases.append(decrypted_alias)

                facts_by_person, _ = await load_recent_person_context(
                    session,
                    person_scope.user_scope,
                    dek,
                    self.container.cipher,
                    [person_scope.person_id],
                )
                scoped_facts = tuple(facts_by_person.get(person_scope.person_id, ()))
                scoped_profile = KnownPersonProfile(
                    person_id=person_row.id,
                    display_name=decrypt_person_display_name(
                        self.container.cipher, dek, person_row
                    ),
                    aliases=tuple(aliases),
                    tier=Tier(person_row.tier),
                    state=PersonState(person_row.state),
                    role_tag=decrypt_person_role_tag(self.container.cipher, dek, person_row),
                    monogram=person_row.monogram,
                    recent_facts=scoped_facts,
                )
                known_names.extend(scoped_profile.all_surface_forms())
                raw_asr_snippets.extend(scoped_facts)
            else:
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
                for profile in known_profiles:
                    known_names.extend(profile.all_surface_forms())
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
            mime_type=audio_info.mime_type,
            known_person_names=[mask_hard_pii(name).masked_text for name in known_names[:60]],
            active_person_name=active_person_name_safe,
            recent_context_snippets=masked_asr_snippets,
        )

        # Call AI Gateway with session=None (AiCallMeter persists in a short post-call Tx; ZERO DB hold!)
        trans_out = await gateway.transcribe(
            scope=scope,
            payload=trans_payload,
        )

        raw_clean = trans_out.raw_transcript.strip()
        corrected_clean = trans_out.corrected_transcript.strip() or raw_clean
        if corrected_clean:
            corrected_clean = apply_spoken_self_correction(corrected_clean)

        return VoiceTranscribeResult(
            transcript=corrected_clean,
            corrected=corrected_clean,
            raw_transcript=raw_clean,
            duration_ms=trans_out.duration_ms,
        )

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
        speech_engine: str | None = None,
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
                return await self._hydrate_capture_response(
                    session,
                    scope=scope,
                    dek=dek,
                    row=capture,
                )

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
            stored_duration_ms = capture.duration_ms
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
        duration_ms: int | None = stored_duration_ms
        screenshot_is_group = False
        safety_decision = SafetyDecision(category=SafetyCategory.NORMAL)
        fallback_error_code: str | None = None
        fallback_override_body: str | None = None
        extraction_mode = "cloud_llm"

        try:
            # 2a. Modality ingestion (4-branch Voice ASR or Screenshot Bubble OCR)
            if source is CaptureSource.VOICE:
                client_transcript = raw_text.strip()
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
                masked_known_names = [mask_hard_pii(name).masked_text for name in known_names[:60]]

                has_audio_asr_fn = getattr(gateway, "has_audio_asr_provider", None)
                has_live_asr = bool(callable(has_audio_asr_fn) and has_audio_asr_fn())

                if audio_object_key and has_live_asr and gateway is not None:
                    # Branch 2: Multimodal cloud ASR (Gemini 3.8 Flash / Qwen3-Omni) with clientTranscript hint
                    audio_bytes, audio_mime = await self.container.storage.get_decrypted(
                        user_id=scope.user_id,
                        dek=dek,
                        object_key=audio_object_key,
                    )
                    trans_payload = TranscribeInput(
                        audio_bytes=audio_bytes,
                        mime_type=audio_mime,
                        known_person_names=masked_known_names,
                        active_person_name=active_person_name_safe,
                        recent_context_snippets=masked_asr_snippets,
                        client_transcript_hint=client_transcript or None,
                        speech_engine=speech_engine,
                    )
                    try:
                        trans_out = await gateway.transcribe(
                            scope=scope,
                            payload=trans_payload,
                        )
                        effective_text = (
                            trans_out.corrected_transcript.strip()
                            or trans_out.raw_transcript.strip()
                            or client_transcript
                        )
                        if trans_out.duration_ms is not None and duration_ms is None:
                            duration_ms = trans_out.duration_ms
                    except Exception as asr_exc:
                        logger.warning(
                            "capture_cloud_asr_failed",
                            capture_id=str(capture_id),
                            exc_type=type(asr_exc).__name__,
                            has_client_transcript=bool(client_transcript),
                        )
                        if client_transcript:
                            effective_text = postprocess_asr_transcript(
                                client_transcript,
                                active_person_name=active_person_name_safe,
                                known_person_names=masked_known_names,
                                recent_context_snippets=masked_asr_snippets,
                            )
                        else:
                            fallback_error_code = "asr_provider_unavailable"
                            fallback_override_body = (
                                "原声录音已妥善保存；本次云端语音转写暂时不可用，请稍后补充文字或重试。"
                            )
                            raise
                elif client_transcript:
                    # Branch 1 & Branch 4: Real on-device native transcript (with or without .m4a audio!)
                    effective_text = postprocess_asr_transcript(
                        client_transcript,
                        active_person_name=active_person_name_safe,
                        known_person_names=masked_known_names,
                        recent_context_snippets=masked_asr_snippets,
                    )
                elif audio_object_key and gateway is not None:
                    # Branch 3: No clientTranscript & no live multimodal ASR configured.
                    # Explicit `TEXT:` fixtures still work; binary audio fails honestly (`asr_provider_unavailable`).
                    audio_bytes, audio_mime = await self.container.storage.get_decrypted(
                        user_id=scope.user_id,
                        dek=dek,
                        object_key=audio_object_key,
                    )
                    trans_payload = TranscribeInput(
                        audio_bytes=audio_bytes,
                        mime_type=audio_mime,
                        known_person_names=masked_known_names,
                        active_person_name=active_person_name_safe,
                        recent_context_snippets=masked_asr_snippets,
                        speech_engine=speech_engine,
                    )
                    try:
                        trans_out = await gateway.transcribe(
                            scope=scope,
                            payload=trans_payload,
                        )
                        effective_text = (
                            trans_out.corrected_transcript.strip()
                            or trans_out.raw_transcript.strip()
                        )
                        if trans_out.duration_ms is not None and duration_ms is None:
                            duration_ms = trans_out.duration_ms
                    except Exception:
                        fallback_error_code = "asr_provider_unavailable"
                        fallback_override_body = (
                            "原声录音已妥善保存；当前尚未配置云端语音转写服务，请稍后补充文字或启用端侧语音听写。"
                        )
                        raise
                else:
                    fallback_error_code = "asr_provider_unavailable"
                    fallback_override_body = (
                        "原声录音已妥善保存；当前尚未配置云端语音转写服务，请稍后补充文字或启用端侧语音听写。"
                    )
                    raise ValueError("No transcript and no audio ASR provider available")

                if effective_text:
                    effective_text = apply_spoken_self_correction(effective_text)
                if duration_ms is None:
                    duration_ms = 4000

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
            try:
                extract_out, prompt_version, model_profile = await gateway.extract_record(
                    scope=scope,
                    payload=extract_in,
                )
            except UpstreamAIError as ext_exc:
                # Real cloud outage fallback -> deterministic local extraction on real speech/text
                if self.container.settings.ai_primary_provider != "fake":
                    logger.warning(
                        "capture_extract_deterministic_fallback",
                        capture_id=str(capture_id),
                        exc_type=type(ext_exc).__name__,
                    )
                    extract_out = extract_deterministically(extract_in)
                    prompt_version = "deterministic_local@1.0.0"
                    model_profile = "deterministic:local"
                    extraction_mode = "deterministic_local"
                else:
                    raise

            if getattr(extract_out, "needs_cloud_refinement", False):
                extraction_mode = "deterministic_local"

            if getattr(extract_out, "repaired_transcript", None):
                repaired = str(extract_out.repaired_transcript).strip()
                if repaired:
                    effective_text = repaired

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
                extraction_mode=extraction_mode,
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
                error_code=fallback_error_code,
                override_body=fallback_override_body,
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
        extraction_mode: str = "cloud_llm",
    ) -> CaptureResponse:
        gate_cfg = self._gate_config()

        async with self.container.db.user_session(scope) as session:
            # Graphiti PR #2154 Admit->Analyze->Commit: serialize per-user commit mutations
            await session.execute(
                sa_text("SELECT pg_advisory_xact_lock(hashtext('mw:commit:' || :uid))"),
                {"uid": str(scope.user_id)},
            )
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
            local_today = now.astimezone(ZoneInfo(tz_name)).date()
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
            candidate_persons_map: OrderedDict[UUID, CaptureCandidatePersonDto] = OrderedDict()

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

                    # Bi-temporal invariant (C1-P0-6): RecordRow.happened_on MUST satisfy <= local_today!
                    historical_dates = [
                        d
                        for f_item, _, _, _ in group_facts
                        if f_item.fact_type
                        not in (
                            FactType.DATE,
                            FactType.PREFERENCE,
                            FactType.TABOO,
                            FactType.WISH,
                        )
                        and (d := _safe_parse_iso_date(f_item.happened_at)) is not None
                        and d <= local_today
                    ]
                    slip_happened_on = min(historical_dates) if historical_dates else local_today

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
                        happened_on=slip_happened_on,
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
                    slip_trigger_dates: list[str] = []
                    slip_self_corrected_from: str | None = None

                    for f_item, decision, _, _ in group_facts:
                        clean_fact_content = strip_forbidden_dev_jargon(f_item.content)
                        if not clean_fact_content:
                            continue
                        fact_id = new_id()
                        parsed_tdate = _safe_parse_iso_date(f_item.trigger_date)
                        if parsed_tdate is not None:
                            slip_trigger_dates.append(parsed_tdate.isoformat())
                        if slip_self_corrected_from is None and f_item.self_corrected_from:
                            slip_self_corrected_from = f_item.self_corrected_from.strip() or None
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

                        # Proposed reminder when date/promise has trigger_date or lunar date;
                        # venting-quarantined captures NEVER spawn active/proposed reminders.
                        if (
                            f_item.fact_type in (FactType.DATE, FactType.PROMISE)
                            and decision.pending_reason is not PendingReason.VENTING
                        ):
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
                        silver_photo_cloaked = (
                            p_prof.tier is Tier.SILVER and record_kind is RecordKind.PHOTO
                        )
                        slip_has_media = bool(media_key and not silver_photo_cloaked)
                        slip_media_url = (
                            self.container.tokens.sign_media_path(
                                f"/v1/materials/{rec_id}/media",
                                user_id=scope.user_id,
                                now=now,
                            )
                            if slip_has_media
                            else None
                        )
                        splits.append(
                            CaptureSplitDto(
                                person_id=pid,
                                person_name=p_prof.display_name,
                                tier=p_prof.tier,
                                is_new_person=pid in newly_created_person_ids,
                                material_id=rec_id,
                                version=1,
                                tag=slip_tag,
                                dur=format_duration_mmss(duration_ms),
                                body=slip_body,
                                snippet=slip_body[:60],
                                trigger_date=(
                                    min(slip_trigger_dates) if slip_trigger_dates else None
                                ),
                                self_corrected_from=slip_self_corrected_from,
                                happened_on=slip_happened_on.isoformat(),
                                kind=record_kind,
                                presentation=pencil_state.presentation,
                                glance_hint=pencil_state.glance_hint,
                                fact_ids=[f.id for f in slip_fact_rows],
                                inbox_item_id=_primary_inbox_id(slip_inbox_ids),
                                has_media=slip_has_media,
                                media_url=slip_media_url,
                            )
                        )

            # -----------------------------------------------------------------
            # Persist Ambiguous Person Disambiguation Groups (held until resolved!)
            # -----------------------------------------------------------------
            for (amb_mention, cand_ids), amb_facts in ambiguous_groups.items():
                for cid in cand_ids:
                    if cid in profiles_by_id and cid not in candidate_persons_map:
                        cp = profiles_by_id[cid]
                        cue_parts = [
                            p_txt
                            for p_txt in (
                                cp.role_tag.strip(),
                                cp.recent_facts[0][:18].strip() if cp.recent_facts else "",
                            )
                            if p_txt
                        ]
                        candidate_persons_map[cid] = CaptureCandidatePersonDto(
                            person_id=cid,
                            display_name=cp.display_name,
                            tier=cp.tier,
                            distinguishing_cue=" · ".join(cue_parts) if cue_parts else None,
                        )
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
                    happened_on=local_today,
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
            candidate_persons_list = list(candidate_persons_map.values())
            needs_refinement = bool(extraction_mode == "deterministic_local")
            has_audio = bool(capture.audio_object_key)
            audio_url = (
                self.container.tokens.sign_media_path(
                    f"/v1/captures/{capture.id}/audio",
                    user_id=scope.user_id,
                    now=now,
                )
                if has_audio
                else None
            )

            summary_blob = {
                "splits": [s.model_dump(mode="json", by_alias=True) for s in splits],
                "candidatePersons": [
                    c.model_dump(mode="json", by_alias=True) for c in candidate_persons_list
                ],
                "pendingCount": total_pending,
                "inboxItemIds": [str(iid) for iid in inbox_item_ids],
                "needsCloudRefinement": needs_refinement,
                "extractionMode": extraction_mode,
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
                candidate_persons=candidate_persons_list,
                pending_count=total_pending,
                inbox_item_ids=inbox_item_ids,
                safety_category=safety_decision.category,
                crisis_card=crisis_dto,
                undo_deadline=undo_deadline,
                needs_cloud_refinement=needs_refinement,
                extraction_mode=extraction_mode,
                has_audio=has_audio,
                audio_url=audio_url,
                duration_ms=duration_ms,
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
        error_code: str | None = None,
        override_body: str | None = None,
    ) -> CaptureResponse:
        gate_cfg = self._gate_config()
        effective_error_code = error_code or PendingReason.EXTRACTION_FAILED.value
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
            body_text = (
                strip_forbidden_dev_jargon(fallback_text.strip())
                or (strip_forbidden_dev_jargon(override_body.strip()) if override_body else "")
                or (
                    "原声录音已妥善保存，稍后可补充归类。"
                    if source is CaptureSource.VOICE
                    else "随手内容已妥善保存。"
                )
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
                        version=1,
                        tag=slip_tag,
                        dur=format_duration_mmss(duration_ms),
                        body=body_text,
                        snippet=body_text[:60],
                        happened_on=rec_row.happened_on.isoformat(),
                        kind=record_kind,
                        fact_ids=[fact_id],
                        inbox_item_id=inbox_id,
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
            has_audio = bool(capture.audio_object_key)
            audio_url = (
                self.container.tokens.sign_media_path(
                    f"/v1/captures/{capture.id}/audio",
                    user_id=scope.user_id,
                    now=now,
                )
                if has_audio
                else None
            )
            summary_blob = {
                "splits": [s.model_dump(mode="json", by_alias=True) for s in splits],
                "pendingCount": 1,
                "inboxItemIds": [str(inbox_id)],
                "errorCode": effective_error_code,
                "crisisCard": (
                    crisis_dto.model_dump(mode="json", by_alias=True)
                    if crisis_dto is not None
                    else None
                ),
            }

            capture.status = CaptureStatus.SAVED_AS_NOTE.value
            capture.error_code = effective_error_code
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
                error_code=effective_error_code,
                has_audio=has_audio,
                audio_url=audio_url,
                duration_ms=duration_ms,
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
    # C1 Recent Captures Manager: GET /captures, GET /captures/{id},
    # GET /captures/{id}/audio, POST /captures/{id}/slips, DELETE /captures/{id},
    # SSE /captures/{id}/events, POST /captures/{id}:undo
    # =========================================================================

    async def list_captures(
        self,
        *,
        scope: UserScope,
        limit: int = 30,
        cursor: str | None = None,
        source: CaptureSource | None = None,
        include_reverted: bool = False,
    ) -> CursorPage[CaptureHistoryItemDto]:
        """List recent captures with live-hydrated slips (4 flat SQL queries, zero N+1)."""
        clamped_limit = max(1, min(100, limit))
        async with self.container.db.user_session(scope) as session:
            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()

            # Query 1: Paginated CaptureRow slice hitting ix_captures_user_created
            stmt = select(CaptureRow).where(CaptureRow.user_id == scope.user_id)
            if source is not None:
                stmt = stmt.where(CaptureRow.source == source.value)
            if not include_reverted:
                stmt = stmt.where(CaptureRow.status != CaptureStatus.REVERTED.value)
            if cursor:
                c_dt, c_id = parse_created_cursor(cursor)
                stmt = stmt.where(tuple_(CaptureRow.created_at, CaptureRow.id) < (c_dt, c_id))
            stmt = stmt.order_by(CaptureRow.created_at.desc(), CaptureRow.id.desc()).limit(
                clamped_limit + 1
            )
            rows = list((await session.scalars(stmt)).all())
            has_more = len(rows) > clamped_limit
            page_rows = rows[:clamped_limit]

            if not page_rows:
                return CursorPage(items=[], next_cursor=None, has_more=False)

            cids = [r.id for r in page_rows]

            # Query 2: All active child RecordRows across this page
            records = list(
                (
                    await session.scalars(
                        select(RecordRow)
                        .where(
                            RecordRow.user_id == scope.user_id,
                            RecordRow.capture_id.in_(cids),
                            RecordRow.deleted_at.is_(None),
                        )
                        .order_by(RecordRow.created_at.asc(), RecordRow.id.asc())
                    )
                ).all()
            )
            records_by_cap: dict[UUID, list[RecordRow]] = defaultdict(list)
            person_ids: set[UUID] = set()
            rec_ids: list[UUID] = []
            for rec in records:
                if rec.capture_id is not None:
                    records_by_cap[rec.capture_id].append(rec)
                person_ids.add(rec.person_id)
                rec_ids.append(rec.id)
            for cap in page_rows:
                if cap.scoped_person_id is not None:
                    person_ids.add(cap.scoped_person_id)

            # Query 3: All referenced PersonRows (live display_name, tier, monogram, state)
            persons_by_id: dict[UUID, PersonRow] = {}
            if person_ids:
                p_rows = list(
                    (
                        await session.scalars(
                            select(PersonRow).where(
                                PersonRow.user_id == scope.user_id,
                                PersonRow.id.in_(person_ids),
                            )
                        )
                    ).all()
                )
                persons_by_id = {p.id: p for p in p_rows}

            person_names: dict[UUID, str] = {}
            for pid, p_row in persons_by_id.items():
                person_names[pid] = decrypt_person_display_name(self.container.cipher, dek, p_row)

            # Query 4: Open InboxItems & live Facts for pencil state derivation
            facts_by_rec: dict[UUID, list[FactRow]] = defaultdict(list)
            open_inbox_by_rec: dict[UUID, dict[InboxItemKind, UUID]] = defaultdict(dict)
            open_inbox_by_cap: dict[UUID, list[UUID]] = defaultdict(list)
            if rec_ids:
                f_rows = list(
                    (
                        await session.scalars(
                            select(FactRow).where(
                                FactRow.user_id == scope.user_id,
                                FactRow.record_id.in_(rec_ids),
                                FactRow.invalid_at.is_(None),
                            )
                        )
                    ).all()
                )
                for f_row in f_rows:
                    if f_row.record_id is not None:
                        facts_by_rec[f_row.record_id].append(f_row)

            inbox_rows = list(
                (
                    await session.scalars(
                        select(InboxItemRow).where(
                            InboxItemRow.user_id == scope.user_id,
                            InboxItemRow.capture_id.in_(cids),
                            InboxItemRow.status == InboxItemStatus.OPEN.value,
                        )
                    )
                ).all()
            )
            for ib in inbox_rows:
                if ib.capture_id is not None:
                    open_inbox_by_cap[ib.capture_id].append(ib.id)
                if ib.record_id is not None:
                    try:
                        open_inbox_by_rec[ib.record_id][InboxItemKind(ib.kind)] = ib.id
                    except ValueError:
                        pass

            items: list[CaptureHistoryItemDto] = []
            for cap in page_rows:
                transcript = (
                    self.container.cipher.decrypt_str(
                        dek,
                        cap.transcript_enc,
                        user_id=cap.user_id,
                        table="captures",
                        column="transcript_enc",
                        row_id=cap.id,
                    )
                    if cap.transcript_enc is not None
                    else ""
                )
                transcript_html = (
                    self.container.cipher.decrypt_str(
                        dek,
                        cap.transcript_marked_enc,
                        user_id=cap.user_id,
                        table="captures",
                        column="transcript_marked_enc",
                        row_id=cap.id,
                    )
                    if cap.transcript_marked_enc is not None
                    else ""
                )
                cap_records = records_by_cap.get(cap.id, [])
                any_silver_person = False
                if cap.scoped_person_id and cap.scoped_person_id in persons_by_id:
                    if persons_by_id[cap.scoped_person_id].tier == Tier.SILVER.value:
                        any_silver_person = True

                live_splits: list[CaptureHistorySplitDto] = []
                person_counts: OrderedDict[UUID, int] = OrderedDict()
                for rec in cap_records:
                    rec_person = persons_by_id.get(rec.person_id)
                    p_name = person_names.get(rec.person_id, "待归档")
                    p_tier = Tier(rec_person.tier) if rec_person is not None else Tier.CRYSTAL
                    if p_tier is Tier.SILVER:
                        any_silver_person = True
                    rec_kind = RecordKind(rec.kind)
                    rec_title = self.container.cipher.decrypt_str(
                        dek,
                        rec.title_enc,
                        user_id=rec.user_id,
                        table="records",
                        column="title_enc",
                        row_id=rec.id,
                    )
                    rec_body = self.container.cipher.decrypt_str(
                        dek,
                        rec.body_enc,
                        user_id=rec.user_id,
                        table="records",
                        column="body_enc",
                        row_id=rec.id,
                    )
                    rec_snippet = (
                        self.container.cipher.decrypt_str(
                            dek,
                            rec.snippet_enc,
                            user_id=rec.user_id,
                            table="records",
                            column="snippet_enc",
                            row_id=rec.id,
                        )
                        if rec.snippet_enc is not None
                        else rec_body[:60]
                    )
                    rec_inbox = open_inbox_by_rec.get(rec.id, {})
                    rec_facts = facts_by_rec.get(rec.id, [])
                    pencil_state = derive_pencil_state(
                        review_state=RecordReviewState(rec.review_state),
                        open_inbox_kinds=tuple(rec_inbox),
                        facts=[_fact_row_to_pencil_view(f) for f in rec_facts],
                        is_new_person=bool(
                            rec_person is not None
                            and rec_person.state == PersonState.PROVISIONAL.value
                        ),
                    )
                    # Silver isolation: cloak photos (`kind == photo`), NEVER cloak voice audio!
                    silver_photo_cloaked = p_tier is Tier.SILVER and rec_kind is RecordKind.PHOTO
                    has_media = bool(rec.media_object_key and not silver_photo_cloaked)
                    media_url = (
                        self.container.tokens.sign_media_path(
                            f"/v1/materials/{rec.id}/media",
                            user_id=scope.user_id,
                            now=now,
                        )
                        if has_media
                        else None
                    )
                    live_splits.append(
                        CaptureHistorySplitDto(
                            material_id=rec.id,
                            version=rec.version,
                            person_id=rec.person_id,
                            person_name=p_name,
                            tier=p_tier,
                            kind=rec_kind,
                            title=rec_title,
                            body=rec_body,
                            snippet=rec_snippet,
                            happened_on=rec.happened_on.isoformat(),
                            presentation=pencil_state.presentation,
                            glance_hint=pencil_state.glance_hint,
                            inbox_item_id=_primary_inbox_id(rec_inbox),
                            fact_ids=[f.id for f in rec_facts],
                            has_media=has_media,
                            media_url=media_url,
                        )
                    )
                    if rec_person is None or rec_person.slug != UNASSIGNED_PERSON_SLUG:
                        person_counts[rec.person_id] = person_counts.get(rec.person_id, 0) + 1

                person_summaries: list[CaptureHistoryPersonSummaryDto] = []
                for pid, cnt in person_counts.items():
                    sum_person = persons_by_id.get(pid)
                    p_name = person_names.get(pid, "待归档")
                    p_tier = Tier(sum_person.tier) if sum_person is not None else Tier.CRYSTAL
                    monogram = (
                        sum_person.monogram
                        if (sum_person and sum_person.monogram)
                        else derive_monogram(p_name)
                    )
                    person_summaries.append(
                        CaptureHistoryPersonSummaryDto(
                            person_id=pid,
                            person_name=p_name,
                            tier=p_tier,
                            monogram=monogram,
                            slip_count=cnt,
                        )
                    )

                has_audio = bool(cap.audio_object_key)
                audio_url = (
                    self.container.tokens.sign_media_path(
                        f"/v1/captures/{cap.id}/audio",
                        user_id=scope.user_id,
                        now=now,
                    )
                    if has_audio
                    else None
                )
                has_image = bool(cap.image_object_key and not any_silver_person)
                first_photo_split = next(
                    (s for s in live_splits if s.kind is RecordKind.PHOTO and s.media_url),
                    None,
                )
                image_url = first_photo_split.media_url if (has_image and first_photo_split) else None
                can_quick_undo = bool(
                    cap.status != CaptureStatus.REVERTED.value
                    and cap.undo_deadline is not None
                    and now <= cap.undo_deadline
                )

                items.append(
                    CaptureHistoryItemDto(
                        capture_id=cap.id,
                        source=CaptureSource(cap.source),
                        route_mode=RouteMode(cap.route_mode),
                        status=CaptureStatus(cap.status),
                        transcript=transcript,
                        transcript_html=transcript_html,
                        has_audio=has_audio,
                        audio_url=audio_url,
                        duration_ms=cap.duration_ms,
                        dur=format_duration_mmss(cap.duration_ms),
                        has_image=has_image,
                        image_url=image_url,
                        can_quick_undo=can_quick_undo,
                        undo_deadline=cap.undo_deadline,
                        active_slip_count=len(live_splits),
                        pending_count=len(open_inbox_by_cap.get(cap.id, [])),
                        person_summaries=person_summaries,
                        splits=live_splits,
                        created_at=cap.created_at,
                        completed_at=cap.completed_at,
                    )
                )

            next_cursor = (
                build_created_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if has_more and page_rows
                else None
            )
            return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

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
            return await self._hydrate_capture_response(
                session,
                scope=scope,
                dek=dek,
                row=capture,
            )

    async def get_capture_audio(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
    ) -> tuple[bytes, str]:
        """Decrypt a capture's original voice audio (releases DB session BEFORE storage I/O)."""
        async with self.container.db.user_session(scope) as session:
            capture = await session.scalar(
                select(CaptureRow).where(
                    CaptureRow.id == capture_id,
                    CaptureRow.user_id == scope.user_id,
                )
            )
            if capture is None or not capture.audio_object_key:
                raise NotFoundError("Capture audio not found")
            if capture.status == CaptureStatus.REVERTED.value:
                raise NotFoundError("Capture audio not found")
            audio_key = capture.audio_object_key
            dek = await self.container.load_user_dek(session, scope)

        return await self.container.storage.get_decrypted(
            user_id=scope.user_id,
            dek=dek,
            object_key=audio_key,
        )

    async def add_slip_to_capture(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        req: CaptureAddSlipRequest,
        idem_key: str,
        route_path: str,
    ) -> tuple[CaptureAddSlipResponse | None, IdempotencyHit | None]:
        """Add a missed slip to an existing non-reverted capture batch (existing or new person)."""
        if req.person_id is None and not (req.new_person_name and req.new_person_name.strip()):
            raise ValidationFailedError("Either personId or newPersonName must be provided")

        req_hash = hash_request_payload(
            {
                "action": "add_slip",
                "captureId": str(capture_id),
                "personId": str(req.person_id) if req.person_id else None,
                "newPersonName": (req.new_person_name or "").strip(),
                "newPersonTier": req.new_person_tier.value,
                "title": req.title or "",
                "body": req.body,
                "happenedOn": req.happened_on.isoformat() if req.happened_on else None,
                "factType": req.fact_type.value,
                "triggerDate": req.trigger_date.isoformat() if req.trigger_date else None,
                "inheritCaptureAudio": req.inherit_capture_audio,
            }
        )

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
                .where(CaptureRow.id == capture_id, CaptureRow.user_id == scope.user_id)
                .with_for_update()
            )
            if capture is None:
                raise NotFoundError("Capture not found")
            if capture.status == CaptureStatus.REVERTED.value:
                raise ConflictError("Cannot add a slip to a reverted capture")

            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)
            local_today = now.astimezone(ZoneInfo(tz_name)).date()

            if req.person_id is not None:
                target_person = await require_person_row(session, scope, req.person_id)
                if (
                    target_person.archived_at is not None
                    or target_person.state == PersonState.ARCHIVED.value
                ):
                    raise ValidationFailedError("Cannot add slip to an archived person")
                if target_person.state == PersonState.PROVISIONAL.value:
                    target_person.state = PersonState.ACTIVE.value
                    target_person.version += 1
                    target_person.updated_at = now
                target_name = decrypt_person_display_name(
                    self.container.cipher, dek, target_person
                )
            else:
                new_name = (req.new_person_name or "").strip()[:80]
                target_person = await create_person_with_primary_alias(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    display_name=new_name,
                    tier=req.new_person_tier,
                    state=PersonState.ACTIVE,
                    now=now,
                )
                target_name = new_name

            clean_body = strip_forbidden_dev_jargon(req.body.strip())
            clean_title = (
                strip_forbidden_dev_jargon(req.title.strip())
                if req.title and req.title.strip()
                else build_natural_slip_tag("", clean_body)
            )
            inherit_audio = bool(req.inherit_capture_audio and capture.audio_object_key)
            rec_kind = RecordKind.VOICE if inherit_audio else RecordKind.NOTE
            media_key = capture.audio_object_key if inherit_audio else None
            slip_happened_on = (
                req.happened_on
                if (req.happened_on is not None and req.happened_on <= local_today)
                else local_today
            )

            rec_id = new_id()
            rec_row = RecordRow(
                id=rec_id,
                user_id=scope.user_id,
                person_id=target_person.id,
                capture_id=capture.id,
                kind=rec_kind.value,
                title_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_title,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=rec_id,
                ),
                body_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_body,
                    user_id=scope.user_id,
                    table="records",
                    column="body_enc",
                    row_id=rec_id,
                ),
                snippet_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_body[:60],
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                media_object_key=media_key,
                audio_duration_ms=capture.duration_ms if inherit_audio else None,
                happened_on=slip_happened_on,
                pinned=False,
                review_state=RecordReviewState.NORMAL.value,
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
                person_id=target_person.id,
                record_id=rec_id,
                capture_id=capture.id,
                type=req.fact_type.value,
                polarity="neutral",
                time_status="current",
                source_speaker=SourceSpeaker.NARRATOR.value,
                content_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_body,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fact_id,
                ),
                trigger_date=req.trigger_date,
                is_lunar=False,
                status=FactStatus.CONFIRMED.value,
                confidence=1.0,
                created_by=CreatedBy.USER.value,
                valid_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(fact_row)
            await session.flush()

            if req.trigger_date is not None:
                await upsert_reminder_for_fact(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    fact=fact_row,
                    fact_content=clean_body,
                    desired_status=ReminderStatus.ACTIVE,
                    now=now,
                    tz_name=tz_name,
                )

            media_url = (
                self.container.tokens.sign_media_path(
                    f"/v1/materials/{rec_id}/media",
                    user_id=scope.user_id,
                    now=now,
                )
                if media_key
                else None
            )
            resp = CaptureAddSlipResponse(
                material_id=rec_id,
                capture_id=capture.id,
                person_id=target_person.id,
                person_name=target_name,
                tier=Tier(target_person.tier),
                kind=rec_kind,
                title=clean_title,
                body=clean_body,
                snippet=clean_body[:60],
                happened_on=slip_happened_on.isoformat(),
                version=1,
                has_media=bool(media_key),
                media_url=media_url,
                affected_person_ids=[target_person.id],
            )
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=201,
                response_body=resp.model_dump(mode="json", by_alias=True),
            )
            return resp, None

    async def delete_capture(
        self,
        *,
        scope: UserScope,
        capture_id: UUID,
        idem_key: str,
        route_path: str,
    ) -> tuple[CaptureUndoResponse | None, IdempotencyHit | None]:
        """Delete an entire capture batch beyond the immediate quick-undo window with refcount-safe blob GC."""
        req_hash = hash_request_payload({"action": "delete_capture", "captureId": str(capture_id)})
        blobs_to_delete: list[str] = []

        async with self.container.db.user_session(scope) as session:
            idem_row, hit = await self.container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="DELETE",
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
            cap_records = list(
                (
                    await session.scalars(
                        select(RecordRow).where(
                            RecordRow.user_id == scope.user_id,
                            RecordRow.capture_id == capture.id,
                        )
                    )
                ).all()
            )
            affected_person_ids = list(dict.fromkeys(r.person_id for r in cap_records))
            reverted_rec_ids = [r.id for r in cap_records]

            candidate_keys: list[str] = []
            for key in (capture.audio_object_key, capture.image_object_key):
                if key and key not in candidate_keys:
                    candidate_keys.append(key)
            for rec in cap_records:
                if rec.media_object_key and rec.media_object_key not in candidate_keys:
                    candidate_keys.append(rec.media_object_key)

            rec_cnt, fact_cnt, rem_cnt = await revert_capture_graph(
                session,
                scope=scope,
                capture=capture,
                now=now,
            )

            # Purge stale chat citations pointing to reverted slips
            if reverted_rec_ids:
                await session.execute(
                    sa_delete(ChatCitationRow).where(
                        ChatCitationRow.user_id == scope.user_id,
                        ChatCitationRow.record_id.in_(reverted_rec_ids),
                    )
                )

            # Detach media keys on this reverted capture & check refcount before deleting blobs
            capture.audio_object_key = None
            capture.image_object_key = None
            await session.flush()

            for key in candidate_keys:
                active_rec_refs = int(
                    await session.scalar(
                        select(func.count(RecordRow.id)).where(
                            RecordRow.user_id == scope.user_id,
                            RecordRow.media_object_key == key,
                            RecordRow.deleted_at.is_(None),
                        )
                    )
                    or 0
                )
                active_cap_refs = int(
                    await session.scalar(
                        select(func.count(CaptureRow.id)).where(
                            CaptureRow.user_id == scope.user_id,
                            CaptureRow.status != CaptureStatus.REVERTED.value,
                            (CaptureRow.audio_object_key == key)
                            | (CaptureRow.image_object_key == key),
                        )
                    )
                    or 0
                )
                if active_rec_refs == 0 and active_cap_refs == 0:
                    blobs_to_delete.append(key)

            resp = CaptureUndoResponse(
                capture_id=capture.id,
                status=CaptureStatus.REVERTED,
                reverted_record_count=rec_cnt,
                rejected_fact_count=fact_cnt,
                cancelled_reminder_count=rem_cnt,
                affected_person_ids=affected_person_ids,
            )
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=200,
                response_body=resp.model_dump(mode="json", by_alias=True),
            )

        # Post-commit blob cleanup (zero zombie blobs, zero partial rollback corruption)
        for obj_key in blobs_to_delete:
            try:
                await self.container.storage.delete_object(object_key=obj_key)
            except Exception as exc:
                logger.warning(
                    "capture_delete_blob_cleanup_failed",
                    capture_id=str(capture_id),
                    exc_type=type(exc).__name__,
                )

        return resp, None

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
                    "The undo window has expired.",
                    error_code="undo_window_expired",
                )

            cap_person_ids = list(
                dict.fromkeys(
                    (
                        await session.scalars(
                            select(RecordRow.person_id).where(
                                RecordRow.user_id == scope.user_id,
                                RecordRow.capture_id == capture.id,
                                RecordRow.deleted_at.is_(None),
                            )
                        )
                    ).all()
                )
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
                affected_person_ids=cap_person_ids,
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

    async def _hydrate_capture_response(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        dek: bytes,
        row: CaptureRow,
    ) -> CaptureResponse:
        """Build live-hydrated `CaptureResponse` (reflecting edits, reassignments & deletes)."""
        base = self._capture_row_to_response(dek, row)
        now = self.container.clock.now()
        has_audio = bool(row.audio_object_key and row.status != CaptureStatus.REVERTED.value)
        audio_url = (
            self.container.tokens.sign_media_path(
                f"/v1/captures/{row.id}/audio",
                user_id=scope.user_id,
                now=now,
            )
            if has_audio
            else None
        )
        if row.status in (CaptureStatus.RECEIVED.value, CaptureStatus.REVERTED.value):
            return base.model_copy(
                update={
                    "has_audio": has_audio,
                    "audio_url": audio_url,
                    "duration_ms": row.duration_ms,
                }
            )

        # Hydrate live non-deleted child records so edits/deletes/reassignments are immediately reflected
        records = list(
            (
                await session.scalars(
                    select(RecordRow)
                    .where(
                        RecordRow.user_id == scope.user_id,
                        RecordRow.capture_id == row.id,
                        RecordRow.deleted_at.is_(None),
                    )
                    .order_by(RecordRow.created_at.asc(), RecordRow.id.asc())
                )
            ).all()
        )
        open_inboxes = list(
            (
                await session.scalars(
                    select(InboxItemRow).where(
                        InboxItemRow.user_id == scope.user_id,
                        InboxItemRow.capture_id == row.id,
                        InboxItemRow.status == InboxItemStatus.OPEN.value,
                    )
                )
            ).all()
        )
        open_inbox_ids = [ib.id for ib in open_inboxes]
        open_inbox_by_rec: dict[UUID, dict[InboxItemKind, UUID]] = defaultdict(dict)
        for ib in open_inboxes:
            if ib.record_id is not None:
                try:
                    open_inbox_by_rec[ib.record_id][InboxItemKind(ib.kind)] = ib.id
                except ValueError:
                    pass

        rec_ids = [r.id for r in records]
        facts_by_rec: dict[UUID, list[FactRow]] = defaultdict(list)
        if rec_ids:
            f_rows = list(
                (
                    await session.scalars(
                        select(FactRow).where(
                            FactRow.user_id == scope.user_id,
                            FactRow.record_id.in_(rec_ids),
                            FactRow.invalid_at.is_(None),
                        )
                    )
                ).all()
            )
            for f_row in f_rows:
                if f_row.record_id is not None:
                    facts_by_rec[f_row.record_id].append(f_row)

        p_ids = {r.person_id for r in records}
        persons_by_id: dict[UUID, PersonRow] = {}
        if p_ids:
            p_rows = list(
                (
                    await session.scalars(
                        select(PersonRow).where(
                            PersonRow.user_id == scope.user_id,
                            PersonRow.id.in_(p_ids),
                        )
                    )
                ).all()
            )
            persons_by_id = {p.id: p for p in p_rows}

        # Preserve prior split metadata (e.g. self_corrected_from) keyed by material_id
        snap_by_mat = {s.material_id: s for s in base.splits}
        live_splits: list[CaptureSplitDto] = []
        for rec in records:
            # Skip held disambiguation placeholder slips if not in splits (matches original capture view)
            p_row = persons_by_id.get(rec.person_id)
            if (
                rec.review_state == RecordReviewState.HELD.value
                and rec.id not in snap_by_mat
                and (p_row is None or p_row.slug == UNASSIGNED_PERSON_SLUG)
            ):
                continue
            p_name = (
                decrypt_person_display_name(self.container.cipher, dek, p_row)
                if p_row is not None
                else "待归档"
            )
            p_tier = Tier(p_row.tier) if p_row is not None else Tier.CRYSTAL
            rec_kind = RecordKind(rec.kind)
            rec_title = self.container.cipher.decrypt_str(
                dek,
                rec.title_enc,
                user_id=rec.user_id,
                table="records",
                column="title_enc",
                row_id=rec.id,
            )
            rec_body = self.container.cipher.decrypt_str(
                dek,
                rec.body_enc,
                user_id=rec.user_id,
                table="records",
                column="body_enc",
                row_id=rec.id,
            )
            rec_snippet = (
                self.container.cipher.decrypt_str(
                    dek,
                    rec.snippet_enc,
                    user_id=rec.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec.id,
                )
                if rec.snippet_enc is not None
                else rec_body[:60]
            )
            rec_inbox = open_inbox_by_rec.get(rec.id, {})
            rec_facts = facts_by_rec.get(rec.id, [])
            pencil_state = derive_pencil_state(
                review_state=RecordReviewState(rec.review_state),
                open_inbox_kinds=tuple(rec_inbox),
                facts=[_fact_row_to_pencil_view(f) for f in rec_facts],
                is_new_person=bool(
                    p_row is not None and p_row.state == PersonState.PROVISIONAL.value
                ),
            )
            trig_dates = [f.trigger_date.isoformat() for f in rec_facts if f.trigger_date is not None]
            silver_photo_cloaked = p_tier is Tier.SILVER and rec_kind is RecordKind.PHOTO
            has_media = bool(rec.media_object_key and not silver_photo_cloaked)
            media_url = (
                self.container.tokens.sign_media_path(
                    f"/v1/materials/{rec.id}/media",
                    user_id=scope.user_id,
                    now=now,
                )
                if has_media
                else None
            )
            prev_snap = snap_by_mat.get(rec.id)
            live_splits.append(
                CaptureSplitDto(
                    person_id=rec.person_id,
                    person_name=p_name,
                    tier=p_tier,
                    is_new_person=bool(
                        p_row is not None and p_row.state == PersonState.PROVISIONAL.value
                    ),
                    material_id=rec.id,
                    version=rec.version,
                    tag=rec_title,
                    dur=format_duration_mmss(rec.audio_duration_ms or row.duration_ms),
                    body=rec_body,
                    snippet=rec_snippet,
                    trigger_date=min(trig_dates) if trig_dates else None,
                    self_corrected_from=prev_snap.self_corrected_from if prev_snap else None,
                    happened_on=rec.happened_on.isoformat(),
                    kind=rec_kind,
                    presentation=pencil_state.presentation,
                    glance_hint=pencil_state.glance_hint,
                    fact_ids=[f.id for f in rec_facts],
                    inbox_item_id=_primary_inbox_id(rec_inbox),
                    has_media=has_media,
                    media_url=media_url,
                )
            )

        return base.model_copy(
            update={
                "splits": live_splits,
                "pending_count": len(open_inbox_ids),
                "inbox_item_ids": open_inbox_ids,
                "has_audio": has_audio,
                "audio_url": audio_url,
                "duration_ms": row.duration_ms,
            }
        )

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
        candidate_persons: list[CaptureCandidatePersonDto] = []
        pending_count = 0
        inbox_item_ids: list[UUID] = []
        crisis_card: CrisisCardDto | None = None
        needs_cloud_refinement = False
        extraction_mode = "cloud_llm"

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
                raw_cands = summary_raw.get("candidatePersons") or []
                candidate_persons = [
                    CaptureCandidatePersonDto.model_validate(item) for item in raw_cands
                ]
                pending_count = int(summary_raw.get("pendingCount") or 0)
                inbox_item_ids = [UUID(str(x)) for x in (summary_raw.get("inboxItemIds") or [])]
                needs_cloud_refinement = bool(summary_raw.get("needsCloudRefinement") or False)
                extraction_mode = str(summary_raw.get("extractionMode") or "cloud_llm")
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
            candidate_persons=candidate_persons,
            pending_count=pending_count,
            inbox_item_ids=inbox_item_ids,
            safety_category=SafetyCategory(row.safety_category),
            crisis_card=crisis_card,
            undo_deadline=row.undo_deadline,
            needs_cloud_refinement=needs_cloud_refinement,
            extraction_mode=extraction_mode,
            error_code=row.error_code,
            has_audio=bool(row.audio_object_key),
            duration_ms=row.duration_ms,
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
    speech_engine_raw = payload.get("speech_engine") if hasattr(payload, "get") else None
    speech_engine = str(speech_engine_raw) if speech_engine_raw else None
    svc = CapturePipelineService(container)
    return await svc.process_capture_by_id(
        scope=UserScope(user_id=user_id),
        capture_id=capture_id,
        ephemeral_image_key=ephemeral_key,
        speech_engine=speech_engine,
    )


def decode_optional_base64(value: str | None, *, field_name: str) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValidationFailedError(f"Invalid base64 encoding for {field_name}") from exc


CaptureService = CapturePipelineService
