"""SQLAlchemy repository for full user data export decryption and crypto-shredding deletion."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.orm import (
    AiCallRow,
    AuthIdentityRow,
    BackgroundJobRow,
    CaptureRow,
    ChatCitationRow,
    ChatMessageRow,
    ChatThreadRow,
    ConsentRow,
    DeviceRow,
    ExportJobRow,
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    ReminderDeliveryRow,
    ReminderRow,
    TelemetryEventRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.shared.scopes import UserScope


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _decrypt_opt_str(
    cipher: EnvelopeCipher,
    dek: bytes,
    ciphertext: bytes | None,
    *,
    user_id: UUID,
    table: str,
    column: str,
    row_id: UUID,
) -> str | None:
    if ciphertext is None:
        return None
    return cipher.decrypt_str(
        dek,
        ciphertext,
        user_id=user_id,
        table=table,
        column=column,
        row_id=row_id,
    )


def _decrypt_opt_json(
    cipher: EnvelopeCipher,
    dek: bytes,
    ciphertext: bytes | None,
    *,
    user_id: UUID,
    table: str,
    column: str,
    row_id: UUID,
) -> Any:
    raw = _decrypt_opt_str(
        cipher,
        dek,
        ciphertext,
        user_id=user_id,
        table=table,
        column=column,
        row_id=row_id,
    )
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class DataRightsRepository:
    """Persistence & field-AAD decryption adapter across all registered user-scoped tables."""

    async def collect_exportable_user_data(
        self,
        user_session: AsyncSession,
        system_session: AsyncSession,
        *,
        scope: UserScope,
        cipher: EnvelopeCipher,
        dek: bytes,
    ) -> tuple[dict[str, Any], list[str]]:
        uid = scope.user_id
        media_keys: list[str] = []
        seen_media: set[str] = set()

        def _track_media(key: str | None) -> None:
            if key and key not in seen_media and "/exports/" not in key:
                seen_media.add(key)
                media_keys.append(key)

        # 1. auth_identities (system table scoped by user_id)
        ident_rows = (
            await system_session.scalars(
                select(AuthIdentityRow)
                .where(AuthIdentityRow.user_id == uid)
                .order_by(AuthIdentityRow.created_at.asc())
            )
        ).all()
        identities = [
            {
                "id": str(r.id),
                "provider": r.provider,
                "contact": _decrypt_opt_str(
                    cipher,
                    dek,
                    r.contact_enc,
                    user_id=uid,
                    table="auth_identities",
                    column="contact_enc",
                    row_id=r.id,
                ),
                "createdAt": r.created_at.isoformat(),
                "lastLoginAt": r.last_login_at.isoformat(),
            }
            for r in ident_rows
        ]

        # 2. user_preferences
        pref_row = await user_session.get(UserPreferenceRow, uid)
        preferences: dict[str, Any] | None = None
        if pref_row is not None:
            _track_media(pref_row.wallpaper_object_key)
            preferences = {
                "preferredEdition": pref_row.preferred_edition,
                "cupidLayout": pref_row.cupid_layout,
                "wallpaperPreset": pref_row.wallpaper_preset,
                "wallpaperObjectKey": pref_row.wallpaper_object_key,
                "wallpaperBlur": pref_row.wallpaper_blur,
                "wallpaperVignette": pref_row.wallpaper_vignette,
                "appLockEnabled": pref_row.app_lock_enabled,
                "lateNightCareEnabled": pref_row.late_night_care_enabled,
                "timezone": pref_row.timezone,
                "version": pref_row.version,
                "updatedAt": pref_row.updated_at.isoformat(),
            }

        # 3. consents
        consent_rows = (
            await user_session.scalars(
                select(ConsentRow)
                .where(ConsentRow.user_id == uid)
                .order_by(ConsentRow.granted_at.asc())
            )
        ).all()
        consents = [
            {
                "id": str(r.id),
                "consentKind": r.consent_kind,
                "policyVersion": r.policy_version,
                "granted": r.granted,
                "grantedAt": r.granted_at.isoformat(),
                "revokedAt": _iso(r.revoked_at),
            }
            for r in consent_rows
        ]

        # 4. devices (metadata ONLY — strictly omit raw push_token_enc & token_hmac!)
        device_rows = (
            await user_session.scalars(
                select(DeviceRow)
                .where(DeviceRow.user_id == uid)
                .order_by(DeviceRow.created_at.asc())
            )
        ).all()
        devices = [
            {
                "id": str(r.id),
                "platform": r.platform,
                "deviceLabel": _decrypt_opt_str(
                    cipher,
                    dek,
                    r.device_label_enc,
                    user_id=uid,
                    table="devices",
                    column="device_label_enc",
                    row_id=r.id,
                ),
                "enabled": r.enabled,
                "createdAt": r.created_at.isoformat(),
                "updatedAt": r.updated_at.isoformat(),
            }
            for r in device_rows
        ]

        # 5. persons
        person_rows = (
            await user_session.scalars(
                select(PersonRow)
                .where(PersonRow.user_id == uid, PersonRow.deleted_at.is_(None))
                .order_by(PersonRow.tier.asc(), PersonRow.rank.asc())
            )
        ).all()
        persons: list[dict[str, Any]] = []
        for p in person_rows:
            _track_media(p.avatar_object_key)
            _track_media(p.main_photo_object_key)
            _track_media(p.sub_photo_object_key)
            persons.append(
                {
                    "id": str(p.id),
                    "slug": p.slug,
                    "tier": p.tier,
                    "rank": p.rank,
                    "state": p.state,
                    "displayName": cipher.decrypt_str(
                        dek,
                        p.display_name_enc,
                        user_id=uid,
                        table="persons",
                        column="display_name_enc",
                        row_id=p.id,
                    ),
                    "monogram": p.monogram,
                    "roleTag": _decrypt_opt_str(
                        cipher,
                        dek,
                        p.role_tag_enc,
                        user_id=uid,
                        table="persons",
                        column="role_tag_enc",
                        row_id=p.id,
                    ),
                    "contextSub": _decrypt_opt_str(
                        cipher,
                        dek,
                        p.context_sub_enc,
                        user_id=uid,
                        table="persons",
                        column="context_sub_enc",
                        row_id=p.id,
                    ),
                    "shortMemo": _decrypt_opt_str(
                        cipher,
                        dek,
                        p.short_memo_enc,
                        user_id=uid,
                        table="persons",
                        column="short_memo_enc",
                        row_id=p.id,
                    ),
                    "traits": _decrypt_opt_json(
                        cipher,
                        dek,
                        p.traits_enc,
                        user_id=uid,
                        table="persons",
                        column="traits_enc",
                        row_id=p.id,
                    ),
                    "hasPhoto": p.has_photo,
                    "avatarObjectKey": p.avatar_object_key,
                    "mainPhotoObjectKey": p.main_photo_object_key,
                    "subPhotoObjectKey": p.sub_photo_object_key,
                    "visualTokens": p.visual_tokens,
                    "version": p.version,
                    "archivedAt": _iso(p.archived_at),
                    "createdAt": p.created_at.isoformat(),
                    "updatedAt": p.updated_at.isoformat(),
                }
            )

        # 6. person_aliases
        alias_rows = (
            await user_session.scalars(
                select(PersonAliasRow)
                .where(PersonAliasRow.user_id == uid)
                .order_by(PersonAliasRow.created_at.asc())
            )
        ).all()
        person_aliases = [
            {
                "id": str(a.id),
                "personId": str(a.person_id),
                "alias": cipher.decrypt_str(
                    dek,
                    a.alias_enc,
                    user_id=uid,
                    table="person_aliases",
                    column="alias_enc",
                    row_id=a.id,
                ),
                "kind": a.kind,
                "isPrimary": a.is_primary,
                "createdAt": a.created_at.isoformat(),
            }
            for a in alias_rows
        ]

        # 7. captures
        capture_rows = (
            await user_session.scalars(
                select(CaptureRow)
                .where(CaptureRow.user_id == uid)
                .order_by(CaptureRow.created_at.asc())
            )
        ).all()
        captures: list[dict[str, Any]] = []
        for c in capture_rows:
            _track_media(c.audio_object_key)
            _track_media(c.image_object_key)
            captures.append(
                {
                    "id": str(c.id),
                    "source": c.source,
                    "routeMode": c.route_mode,
                    "scopedPersonId": str(c.scoped_person_id) if c.scoped_person_id else None,
                    "status": c.status,
                    "audioObjectKey": c.audio_object_key,
                    "imageObjectKey": c.image_object_key,
                    "keepOriginalMedia": c.keep_original_media,
                    "durationMs": c.duration_ms,
                    "rawText": _decrypt_opt_str(
                        cipher,
                        dek,
                        c.raw_text_enc,
                        user_id=uid,
                        table="captures",
                        column="raw_text_enc",
                        row_id=c.id,
                    ),
                    "transcript": _decrypt_opt_str(
                        cipher,
                        dek,
                        c.transcript_enc,
                        user_id=uid,
                        table="captures",
                        column="transcript_enc",
                        row_id=c.id,
                    ),
                    "transcriptMarked": _decrypt_opt_str(
                        cipher,
                        dek,
                        c.transcript_marked_enc,
                        user_id=uid,
                        table="captures",
                        column="transcript_marked_enc",
                        row_id=c.id,
                    ),
                    "splitsSummary": _decrypt_opt_json(
                        cipher,
                        dek,
                        c.splits_summary_enc,
                        user_id=uid,
                        table="captures",
                        column="splits_summary_enc",
                        row_id=c.id,
                    ),
                    "safetyCategory": c.safety_category,
                    "promptVersion": c.prompt_version,
                    "modelProfile": c.model_profile,
                    "undoDeadline": _iso(c.undo_deadline),
                    "errorCode": c.error_code,
                    "createdAt": c.created_at.isoformat(),
                    "completedAt": _iso(c.completed_at),
                }
            )

        # 8. records -> materials
        record_rows = (
            await user_session.scalars(
                select(RecordRow)
                .where(RecordRow.user_id == uid, RecordRow.deleted_at.is_(None))
                .order_by(RecordRow.created_at.asc())
            )
        ).all()
        materials: list[dict[str, Any]] = []
        for rec in record_rows:
            _track_media(rec.media_object_key)
            materials.append(
                {
                    "id": str(rec.id),
                    "personId": str(rec.person_id),
                    "captureId": str(rec.capture_id) if rec.capture_id else None,
                    "kind": rec.kind,
                    "title": cipher.decrypt_str(
                        dek,
                        rec.title_enc,
                        user_id=uid,
                        table="records",
                        column="title_enc",
                        row_id=rec.id,
                    ),
                    "body": cipher.decrypt_str(
                        dek,
                        rec.body_enc,
                        user_id=uid,
                        table="records",
                        column="body_enc",
                        row_id=rec.id,
                    ),
                    "snippet": _decrypt_opt_str(
                        cipher,
                        dek,
                        rec.snippet_enc,
                        user_id=uid,
                        table="records",
                        column="snippet_enc",
                        row_id=rec.id,
                    ),
                    "mediaObjectKey": rec.media_object_key,
                    "audioOffsetMs": rec.audio_offset_ms,
                    "audioDurationMs": rec.audio_duration_ms,
                    "stampCode": rec.stamp_code,
                    "happenedOn": rec.happened_on.isoformat() if rec.happened_on else None,
                    "pinned": rec.pinned,
                    "reviewState": rec.review_state,
                    "version": rec.version,
                    "createdAt": rec.created_at.isoformat(),
                    "updatedAt": rec.updated_at.isoformat(),
                }
            )

        # 9. facts
        fact_rows = (
            await user_session.scalars(
                select(FactRow).where(FactRow.user_id == uid).order_by(FactRow.created_at.asc())
            )
        ).all()
        facts = [
            {
                "id": str(f.id),
                "personId": str(f.person_id) if f.person_id else None,
                "recordId": str(f.record_id) if f.record_id else None,
                "captureId": str(f.capture_id) if f.capture_id else None,
                "type": f.type,
                "polarity": f.polarity,
                "timeStatus": f.time_status,
                "sourceSpeaker": f.source_speaker,
                "content": cipher.decrypt_str(
                    dek,
                    f.content_enc,
                    user_id=uid,
                    table="facts",
                    column="content_enc",
                    row_id=f.id,
                ),
                "triggerDate": f.trigger_date.isoformat() if f.trigger_date else None,
                "triggerTime": f.trigger_time.isoformat() if f.trigger_time else None,
                "isLunar": f.is_lunar,
                "lunarMonth": f.lunar_month,
                "lunarDay": f.lunar_day,
                "lunarIsLeapMonth": f.lunar_is_leap_month,
                "status": f.status,
                "pendingReason": f.pending_reason,
                "confidence": f.confidence,
                "createdBy": f.created_by,
                "validAt": f.valid_at.isoformat(),
                "invalidAt": _iso(f.invalid_at),
                "supersededById": str(f.superseded_by_id) if f.superseded_by_id else None,
                "promptVersion": f.prompt_version,
                "version": f.version,
                "createdAt": f.created_at.isoformat(),
                "updatedAt": f.updated_at.isoformat(),
            }
            for f in fact_rows
        ]

        # 10. inbox_items
        inbox_rows = (
            await user_session.scalars(
                select(InboxItemRow)
                .where(InboxItemRow.user_id == uid)
                .order_by(InboxItemRow.created_at.asc())
            )
        ).all()
        inbox_items = [
            {
                "id": str(item.id),
                "personId": str(item.person_id) if item.person_id else None,
                "captureId": str(item.capture_id) if item.capture_id else None,
                "recordId": str(item.record_id) if item.record_id else None,
                "kind": item.kind,
                "status": item.status,
                "factIds": [str(fid) for fid in item.fact_ids],
                "candidatePersonIds": [str(cid) for cid in item.candidate_person_ids],
                "payload": _decrypt_opt_json(
                    cipher,
                    dek,
                    item.payload_enc,
                    user_id=uid,
                    table="inbox_items",
                    column="payload_enc",
                    row_id=item.id,
                ),
                "resolutionAction": item.resolution_action,
                "expiresAt": _iso(item.expires_at),
                "resolvedAt": _iso(item.resolved_at),
                "createdAt": item.created_at.isoformat(),
            }
            for item in inbox_rows
        ]

        # 11. reminders
        reminder_rows = (
            await user_session.scalars(
                select(ReminderRow)
                .where(ReminderRow.user_id == uid)
                .order_by(ReminderRow.created_at.asc())
            )
        ).all()
        reminders = [
            {
                "id": str(rem.id),
                "personId": str(rem.person_id),
                "factId": str(rem.fact_id) if rem.fact_id else None,
                "title": cipher.decrypt_str(
                    dek,
                    rem.title_enc,
                    user_id=uid,
                    table="reminders",
                    column="title_enc",
                    row_id=rem.id,
                ),
                "rule": rem.rule,
                "isLunar": rem.is_lunar,
                "targetMonth": rem.target_month,
                "targetDay": rem.target_day,
                "isLeapMonth": rem.is_leap_month,
                "onceDate": rem.once_date.isoformat() if rem.once_date else None,
                "fireHour": rem.fire_hour,
                "fireMinute": rem.fire_minute,
                "leadDays": list(rem.lead_days),
                "timezone": rem.timezone,
                "nextFireAt": _iso(rem.next_fire_at),
                "nextLeadDay": rem.next_lead_day,
                "status": rem.status,
                "lastFiredAt": _iso(rem.last_fired_at),
                "actionDoneAt": _iso(rem.action_done_at),
                "version": rem.version,
                "createdAt": rem.created_at.isoformat(),
                "updatedAt": rem.updated_at.isoformat(),
            }
            for rem in reminder_rows
        ]

        # 12. reminder_deliveries
        delivery_rows = (
            await user_session.scalars(
                select(ReminderDeliveryRow)
                .where(ReminderDeliveryRow.user_id == uid)
                .order_by(ReminderDeliveryRow.delivered_at.asc())
            )
        ).all()
        reminder_deliveries = [
            {
                "id": str(d.id),
                "reminderId": str(d.reminder_id),
                "scheduledFor": d.scheduled_for.isoformat(),
                "leadDay": d.lead_day,
                "channel": d.channel,
                "status": d.status,
                "deliveredAt": d.delivered_at.isoformat(),
            }
            for d in delivery_rows
        ]

        # 13. chat_threads
        thread_rows = (
            await user_session.scalars(
                select(ChatThreadRow)
                .where(ChatThreadRow.user_id == uid)
                .order_by(ChatThreadRow.created_at.asc())
            )
        ).all()
        chat_threads = [
            {
                "id": str(t.id),
                "personId": str(t.person_id),
                "rollingSummary": _decrypt_opt_str(
                    cipher,
                    dek,
                    t.rolling_summary_enc,
                    user_id=uid,
                    table="chat_threads",
                    column="rolling_summary_enc",
                    row_id=t.id,
                ),
                "messageCount": t.message_count,
                "sessionStartedAt": _iso(t.session_started_at),
                "lastUserMessageAt": _iso(t.last_user_message_at),
                "createdAt": t.created_at.isoformat(),
                "updatedAt": t.updated_at.isoformat(),
            }
            for t in thread_rows
        ]

        # 14. chat_messages
        message_rows = (
            await user_session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.user_id == uid)
                .order_by(ChatMessageRow.created_at.asc())
            )
        ).all()
        chat_messages = [
            {
                "id": str(m.id),
                "threadId": str(m.thread_id),
                "personId": str(m.person_id),
                "role": m.role,
                "content": cipher.decrypt_str(
                    dek,
                    m.content_enc,
                    user_id=uid,
                    table="chat_messages",
                    column="content_enc",
                    row_id=m.id,
                ),
                "whisper": _decrypt_opt_str(
                    cipher,
                    dek,
                    m.whisper_enc,
                    user_id=uid,
                    table="chat_messages",
                    column="whisper_enc",
                    row_id=m.id,
                ),
                "perspectives": _decrypt_opt_json(
                    cipher,
                    dek,
                    m.perspectives_enc,
                    user_id=uid,
                    table="chat_messages",
                    column="perspectives_enc",
                    row_id=m.id,
                ),
                "suggestions": _decrypt_opt_json(
                    cipher,
                    dek,
                    m.suggestions_enc,
                    user_id=uid,
                    table="chat_messages",
                    column="suggestions_enc",
                    row_id=m.id,
                ),
                "referencedRecordIds": [str(rid) for rid in m.referenced_record_ids],
                "tierSnapshot": m.tier_snapshot,
                "safetyCategory": m.safety_category,
                "hasUnrecordedGap": m.has_unrecorded_gap,
                "promptVersion": m.prompt_version,
                "clientMessageId": m.client_message_id,
                "createdAt": m.created_at.isoformat(),
            }
            for m in message_rows
        ]

        # 15. chat_citations
        citation_rows = (
            await user_session.scalars(
                select(ChatCitationRow)
                .where(ChatCitationRow.user_id == uid)
                .order_by(ChatCitationRow.created_at.asc(), ChatCitationRow.citation_index.asc())
            )
        ).all()
        chat_citations = [
            {
                "id": str(cit.id),
                "messageId": str(cit.message_id),
                "recordId": str(cit.record_id),
                "citationIndex": cit.citation_index,
                "createdAt": cit.created_at.isoformat(),
            }
            for cit in citation_rows
        ]

        # 16. ai_calls -> aiUsageSummary
        ai_rows = (
            await user_session.scalars(
                select(AiCallRow)
                .where(AiCallRow.user_id == uid)
                .order_by(AiCallRow.created_at.asc())
            )
        ).all()
        by_task: dict[str, int] = {}
        for call in ai_rows:
            by_task[call.task] = by_task.get(call.task, 0) + 1
        ai_usage_summary = {
            "totalCalls": len(ai_rows),
            "totalInputTokens": sum(c.input_tokens for c in ai_rows),
            "totalOutputTokens": sum(c.output_tokens for c in ai_rows),
            "totalCostMicrosCny": sum(c.cost_micros_cny for c in ai_rows),
            "callsByTask": by_task,
            "calls": [
                {
                    "id": str(c.id),
                    "task": c.task,
                    "provider": c.provider,
                    "model": c.model,
                    "promptId": c.prompt_id,
                    "promptVersion": c.prompt_version,
                    "inputTokens": c.input_tokens,
                    "outputTokens": c.output_tokens,
                    "latencyMs": c.latency_ms,
                    "costMicrosCny": c.cost_micros_cny,
                    "outcome": c.outcome,
                    "createdAt": c.created_at.isoformat(),
                }
                for c in ai_rows
            ],
        }

        # 17. telemetry_events
        telemetry_rows = (
            await user_session.scalars(
                select(TelemetryEventRow)
                .where(TelemetryEventRow.user_id == uid)
                .order_by(TelemetryEventRow.occurred_at.asc())
            )
        ).all()
        telemetry_events = [
            {
                "id": str(t.id),
                "eventName": t.event_name,
                "metadata": t.metadata_json,
                "occurredAt": t.occurred_at.isoformat(),
                "createdAt": t.created_at.isoformat(),
            }
            for t in telemetry_rows
        ]

        sections: dict[str, Any] = {
            "identities": identities,
            "preferences": preferences,
            "consents": consents,
            "devices": devices,
            "persons": persons,
            "personAliases": person_aliases,
            "captures": captures,
            "materials": materials,
            "facts": facts,
            "inboxItems": inbox_items,
            "reminders": reminders,
            "reminderDeliveries": reminder_deliveries,
            "chatThreads": chat_threads,
            "chatMessages": chat_messages,
            "chatCitations": chat_citations,
            "aiUsageSummary": ai_usage_summary,
            "telemetryEvents": telemetry_events,
        }
        return sections, media_keys

    async def create_export_job(
        self,
        session: AsyncSession,
        *,
        export_id: UUID,
        scope: UserScope,
        status: str,
        created_at: datetime,
        download_token_hash: bytes | None = None,
    ) -> ExportJobRow:
        row = ExportJobRow(
            id=export_id,
            user_id=scope.user_id,
            status=status,
            download_token_hash=download_token_hash,
            created_at=created_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_export_job(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        export_id: UUID,
        for_update: bool = False,
    ) -> ExportJobRow | None:
        stmt = select(ExportJobRow).where(
            ExportJobRow.id == export_id,
            ExportJobRow.user_id == scope.user_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def list_export_jobs(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
    ) -> list[ExportJobRow]:
        rows = (
            await session.scalars(
                select(ExportJobRow)
                .where(ExportJobRow.user_id == scope.user_id)
                .order_by(ExportJobRow.created_at.desc())
            )
        ).all()
        return list(rows)

    async def resolve_export_owner_via_system(
        self,
        system_session: AsyncSession,
        *,
        export_id: UUID,
    ) -> UUID | None:
        return await system_session.scalar(
            select(ExportJobRow.user_id)
            .join(UserRow, UserRow.id == ExportJobRow.user_id)
            .where(
                ExportJobRow.id == export_id,
                UserRow.status == "active",
                UserRow.deleted_at.is_(None),
            )
        )

    async def consume_download_token_once(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        export_id: UUID,
        downloaded_at: datetime,
    ) -> bool:
        result = await session.execute(
            update(ExportJobRow)
            .where(
                ExportJobRow.id == export_id,
                ExportJobRow.user_id == scope.user_id,
                ExportJobRow.downloaded_at.is_(None),
                ExportJobRow.download_token_hash.is_not(None),
            )
            .values(
                downloaded_at=downloaded_at,
                download_token_hash=None,
            )
            .returning(ExportJobRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def shred_user_key_in_session(
        self,
        user_session: AsyncSession,
        *,
        scope: UserScope,
        destroyed_at: datetime,
    ) -> None:
        key_row = await user_session.get(UserKeyRow, scope.user_id)
        if key_row is not None:
            key_row.wrapped_dek = None
            key_row.destroyed_at = destroyed_at
            await user_session.flush()

    async def cancel_queued_jobs_and_delete_user(
        self,
        system_session: AsyncSession,
        *,
        user_id: UUID,
        now: datetime,
    ) -> int:
        cancelled = await system_session.execute(
            update(BackgroundJobRow)
            .where(
                BackgroundJobRow.user_id == user_id,
                BackgroundJobRow.status.in_(["queued", "running"]),
            )
            .values(
                status="cancelled",
                payload_json={},
                locked_until=None,
                last_error="cancelled_by_account_deletion",
                updated_at=now,
            )
            .returning(BackgroundJobRow.id)
        )
        cancelled_count = len(cancelled.scalars().all())
        await system_session.execute(delete(UserRow).where(UserRow.id == user_id))
        await system_session.flush()
        return cancelled_count
