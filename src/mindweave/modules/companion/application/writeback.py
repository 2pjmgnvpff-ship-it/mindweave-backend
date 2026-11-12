"""Async chat-to-scrapbook fact write-back & rolling thread summarization handlers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

import mindweave.modules.capture as _capture_pkg  # noqa: F401
from mindweave.ai.ports import ModelGatewayPort
from mindweave.ai.schemas.contracts import ExtractRecordInput, KnownPersonRef
from mindweave.core.clock import to_user_local
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import NotFoundError, UpstreamAIError
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    ChatMessageRow,
    ChatThreadRow,
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    UserPreferenceRow,
)
from mindweave.modules.companion.domain.output_guard import sanitize_text
from mindweave.modules.companion.domain.tier_policy import decide_chat_writeback_gate
from mindweave.modules.memory.infra.reminders_helper import upsert_reminder_for_fact
from mindweave.modules.memory.infra.repository import (
    apply_bitemporal_supersession_for_fact,
    check_polarity_flip_for_person,
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
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderStatus,
    RouteMode,
    SafetyCategory,
    SourceSpeaker,
    Tier,
    TimeStatus,
)
from mindweave.shared.pii import mask_hard_pii
from mindweave.shared.scopes import PersonScope
from mindweave.workers.ports import TASK_SUMMARIZE_THREAD, TASK_WRITEBACK_CHAT_FACTS


def _resolve_gateway(
    container: AppContainer,
    explicit: ModelGatewayPort | None = None,
) -> ModelGatewayPort:
    if explicit is not None:
        return explicit
    for key in ("model_gateway", "gateway", "ai_gateway"):
        candidate = container.extras.get(key)
        if candidate is not None:
            return candidate  # type: ignore[no-any-return]
    raise UpstreamAIError("ModelGatewayPort is not configured on AppContainer")


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class WritebackResult:
    capture_id: UUID
    record_ids: list[UUID]
    confirmed_fact_ids: list[UUID]
    pending_fact_ids: list[UUID]
    inbox_item_ids: list[UUID]
    status: str


async def writeback_facts_from_chat_message(
    container: AppContainer,
    *,
    scope: PersonScope,
    message_id: UUID,
    model_gateway: ModelGatewayPort | None = None,
) -> WritebackResult:
    """Extract personal/relationship facts from a user's chat message into the scrapbook via GatePolicy.

    Idempotent on `(user_id, f"chat-writeback:{message_id}")` via `captures.uq_captures_user_idem`.
    Strictly preserves Invariants I1, I2, I3, I6, I10, and I12.
    """
    gateway = _resolve_gateway(container, model_gateway)
    idem_key = f"chat-writeback:{message_id}"

    async with container.db.user_session(scope.user_scope) as session:
        existing_capture = await session.scalar(
            select(CaptureRow).where(
                CaptureRow.user_id == scope.user_id,
                CaptureRow.idempotency_key == idem_key,
            )
        )
        if existing_capture is not None and existing_capture.status in (
            CaptureStatus.COMPLETED.value,
            CaptureStatus.NEEDS_REVIEW.value,
            CaptureStatus.SAVED_AS_NOTE.value,
        ):
            rec_ids = list(
                (
                    await session.scalars(
                        select(RecordRow.id).where(
                            RecordRow.user_id == scope.user_id,
                            RecordRow.capture_id == existing_capture.id,
                        )
                    )
                ).all()
            )
            fact_rows = list(
                (
                    await session.scalars(
                        select(FactRow).where(
                            FactRow.user_id == scope.user_id,
                            FactRow.capture_id == existing_capture.id,
                        )
                    )
                ).all()
            )
            inbox_ids = list(
                (
                    await session.scalars(
                        select(InboxItemRow.id).where(
                            InboxItemRow.user_id == scope.user_id,
                            InboxItemRow.capture_id == existing_capture.id,
                        )
                    )
                ).all()
            )
            return WritebackResult(
                capture_id=existing_capture.id,
                record_ids=rec_ids,
                confirmed_fact_ids=[
                    f.id for f in fact_rows if f.status == FactStatus.CONFIRMED.value
                ],
                pending_fact_ids=[f.id for f in fact_rows if f.status == FactStatus.PENDING.value],
                inbox_item_ids=inbox_ids,
                status=existing_capture.status,
            )

        person_row = await session.scalar(
            select(PersonRow).where(
                PersonRow.user_id == scope.user_id,
                PersonRow.id == scope.person_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
            )
        )
        if person_row is None:
            raise NotFoundError("Person not found for chat fact write-back")

        msg_row = await session.scalar(
            select(ChatMessageRow).where(
                ChatMessageRow.user_id == scope.user_id,
                ChatMessageRow.person_id == scope.person_id,
                ChatMessageRow.id == message_id,
            )
        )
        if msg_row is None:
            raise NotFoundError("Chat message not found for fact write-back")

        dek = await container.load_user_dek(session, scope.user_scope)
        now = container.clock.now()

        pref_row = await session.scalar(
            select(UserPreferenceRow).where(UserPreferenceRow.user_id == scope.user_id)
        )
        tz_name = pref_row.timezone if pref_row is not None else container.settings.default_timezone
        local_today = to_user_local(now, tz_name).date()

        raw_user_text = container.cipher.decrypt_str(
            dek,
            msg_row.content_enc,
            user_id=scope.user_id,
            table="chat_messages",
            column="content_enc",
            row_id=msg_row.id,
        )

        # Persist CaptureRow(source='chat') FIRST (Invariant I2)
        capture_id = existing_capture.id if existing_capture is not None else new_id()
        if existing_capture is None:
            raw_enc = container.cipher.encrypt_str(
                dek,
                raw_user_text,
                user_id=scope.user_id,
                table="captures",
                column="raw_text_enc",
                row_id=capture_id,
            )
            capture_row = CaptureRow(
                id=capture_id,
                user_id=scope.user_id,
                source=CaptureSource.CHAT.value,
                route_mode=RouteMode.CURRENT.value,
                scoped_person_id=scope.person_id,
                status=CaptureStatus.EXTRACTING.value,
                raw_text_enc=raw_enc,
                safety_category=msg_row.safety_category,
                idempotency_key=idem_key,
                created_at=now,
            )
            session.add(capture_row)
            await session.flush()
        else:
            capture_row = existing_capture

        # Load user's active/provisional persons (excluding unassigned holding bucket) + aliases
        all_persons = list(
            (
                await session.scalars(
                    select(PersonRow).where(
                        PersonRow.user_id == scope.user_id,
                        PersonRow.deleted_at.is_(None),
                        PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
                    )
                )
            ).all()
        )
        all_aliases = list(
            (
                await session.scalars(
                    select(PersonAliasRow).where(PersonAliasRow.user_id == scope.user_id)
                )
            ).all()
        )
        aliases_by_person: dict[UUID, list[str]] = defaultdict(list)
        for arow in all_aliases:
            alias_str = container.cipher.decrypt_str(
                dek,
                arow.alias_enc,
                user_id=scope.user_id,
                table="person_aliases",
                column="alias_enc",
                row_id=arow.id,
            )
            aliases_by_person[arow.person_id].append(alias_str)

        persons_by_id: dict[UUID, PersonRow] = {p.id: p for p in all_persons}
        person_names_by_id: dict[UUID, str] = {}
        name_to_person_ids: dict[str, list[UUID]] = defaultdict(list)
        known_refs: list[KnownPersonRef] = []

        for prow in all_persons:
            pname = container.cipher.decrypt_str(
                dek,
                prow.display_name_enc,
                user_id=scope.user_id,
                table="persons",
                column="display_name_enc",
                row_id=prow.id,
            )
            person_names_by_id[prow.id] = pname
            prole = (
                container.cipher.decrypt_str(
                    dek,
                    prow.role_tag_enc,
                    user_id=scope.user_id,
                    table="persons",
                    column="role_tag_enc",
                    row_id=prow.id,
                )
                if prow.role_tag_enc is not None
                else ""
            )
            p_aliases = aliases_by_person.get(prow.id, [])
            for label in [pname, *p_aliases]:
                norm = label.strip().lower()
                if norm and prow.id not in name_to_person_ids[norm]:
                    name_to_person_ids[norm].append(prow.id)

            known_refs.append(
                KnownPersonRef(
                    person_id=prow.id,
                    display_name=mask_hard_pii(pname).masked_text,
                    aliases=[mask_hard_pii(a).masked_text for a in p_aliases],
                    tier=Tier.from_any(prow.tier),
                    role_tag=mask_hard_pii(prole).masked_text,
                )
            )

        scoped_tier = Tier.from_any(person_row.tier)
        extract_input = ExtractRecordInput(
            text=mask_hard_pii(raw_user_text).masked_text,
            current_date=local_today.isoformat(),
            route_mode=RouteMode.CURRENT.value,
            scoped_person_id=scope.person_id,
            scoped_person_tier=scoped_tier,
            input_source=CaptureSource.CHAT.value,
            known_persons=known_refs,
        )

        record_ids: list[UUID] = []
        confirmed_fact_ids: list[UUID] = []
        pending_fact_ids: list[UUID] = []
        inbox_item_ids: list[UUID] = []

        try:
            extract_out, prompt_ver, model_prof = await gateway.extract_record(
                scope=scope.user_scope,
                payload=extract_input,
                session=session,
            )
        except Exception:
            # Invariant I2 graceful degradation: never lose user input on extraction failure
            rec_id = new_id()
            fallback_title = "聊天随手备忘"
            snippet_str = (
                sanitize_text(" ".join(raw_user_text.split())[:80], allow_empty=True).cleaned_text
                or fallback_title
            )
            rec_row = RecordRow(
                id=rec_id,
                user_id=scope.user_id,
                person_id=scope.person_id,
                capture_id=capture_id,
                kind=RecordKind.NOTE.value,
                title_enc=container.cipher.encrypt_str(
                    dek,
                    fallback_title,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=rec_id,
                ),
                body_enc=container.cipher.encrypt_str(
                    dek,
                    raw_user_text,
                    user_id=scope.user_id,
                    table="records",
                    column="body_enc",
                    row_id=rec_id,
                ),
                snippet_enc=container.cipher.encrypt_str(
                    dek,
                    snippet_str,
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                happened_on=local_today,
                pinned=True,
                review_state=RecordReviewState.NORMAL.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(rec_row)

            fid = new_id()
            frow = FactRow(
                id=fid,
                user_id=scope.user_id,
                person_id=scope.person_id,
                record_id=rec_id,
                capture_id=capture_id,
                type=FactType.NOTE.value,
                polarity=Polarity.NEUTRAL.value,
                time_status=TimeStatus.CURRENT.value,
                source_speaker=SourceSpeaker.NARRATOR.value,
                content_enc=container.cipher.encrypt_str(
                    dek,
                    raw_user_text,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fid,
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
            session.add(frow)

            iid = new_id()
            scoped_pname = person_names_by_id.get(scope.person_id, "")
            irow = InboxItemRow(
                id=iid,
                user_id=scope.user_id,
                person_id=scope.person_id,
                capture_id=capture_id,
                record_id=rec_id,
                kind=InboxItemKind.FACT_CONFIRM.value,
                status=InboxItemStatus.OPEN.value,
                fact_ids=[fid],
                candidate_person_ids=[scope.person_id],
                payload_enc=container.cipher.encrypt_json(
                    dek,
                    {
                        "summary": snippet_str,
                        "title": fallback_title,
                        "content": snippet_str,
                        "factType": FactType.NOTE.value,
                        "personName": scoped_pname,
                        "pendingReason": PendingReason.EXTRACTION_FAILED.value,
                    },
                    user_id=scope.user_id,
                    table="inbox_items",
                    column="payload_enc",
                    row_id=iid,
                ),
                created_at=now,
            )
            session.add(irow)

            capture_row.status = CaptureStatus.SAVED_AS_NOTE.value
            capture_row.error_code = "extraction_failed"
            capture_row.completed_at = now
            await session.flush()

            return WritebackResult(
                capture_id=capture_id,
                record_ids=[rec_id],
                confirmed_fact_ids=[],
                pending_fact_ids=[fid],
                inbox_item_ids=[iid],
                status=capture_row.status,
            )

        capture_row.prompt_version = prompt_ver
        capture_row.model_profile = model_prof
        capture_row.safety_category = extract_out.safety_flag.value

        if (
            extract_out.crisis_flag
            or extract_out.safety_flag in (SafetyCategory.CRISIS, SafetyCategory.VIOLENCE_VICTIM)
            or not extract_out.facts
        ):
            capture_row.status = CaptureStatus.COMPLETED.value
            capture_row.completed_at = now
            await session.flush()
            return WritebackResult(
                capture_id=capture_id,
                record_ids=[],
                confirmed_fact_ids=[],
                pending_fact_ids=[],
                inbox_item_ids=[],
                status=capture_row.status,
            )

        # Group extracted facts by resolved target person (current person locked by default;
        # mentions of OTHER existing persons split out to their pending queue per Master Plan §3.1)
        candidate_lookup = {c.mention.strip().lower(): c for c in extract_out.persons}
        distinct_targets: set[UUID] = set()
        resolved_items: list[tuple[UUID, list[UUID], bool, Any]] = []

        for item in extract_out.facts:
            mention_norm = item.person_mention.strip().lower()
            cand = candidate_lookup.get(mention_norm)

            if item.person_id is not None and item.person_id in persons_by_id:
                target_pid = item.person_id
                cands = [target_pid]
            elif (
                cand is not None
                and cand.matched_person_id is not None
                and cand.matched_person_id in persons_by_id
            ):
                target_pid = cand.matched_person_id
                cands = [cid for cid in cand.candidate_person_ids if cid in persons_by_id] or [
                    target_pid
                ]
            elif mention_norm in name_to_person_ids:
                matched_ids = name_to_person_ids[mention_norm]
                target_pid = matched_ids[0] if len(matched_ids) == 1 else scope.person_id
                cands = matched_ids
            else:
                # Default to current scoped person in chat!
                target_pid = scope.person_id
                cands = [scope.person_id]

            distinct_targets.add(target_pid)
            is_locked_to_current = target_pid == scope.person_id and len(cands) == 1
            resolved_items.append((target_pid, cands, is_locked_to_current, item))

        multi_person = len(distinct_targets) > 1
        records_by_person: dict[UUID, RecordRow] = {}

        for target_pid, cands, is_locked_to_current, item in resolved_items:
            target_person = persons_by_id[target_pid]
            target_tier = Tier.from_any(target_person.tier)
            target_state = PersonState(target_person.state)

            rec_row_existing = records_by_person.get(target_pid)
            if rec_row_existing is None:
                rec_id = new_id()
                raw_title = item.title.strip() or item.content.strip()[:24] or "手帐便签"
                clean_title = sanitize_text(raw_title, allow_empty=True).cleaned_text or "手帐便签"
                clean_body = (
                    sanitize_text(raw_user_text, allow_empty=True).cleaned_text or raw_user_text
                )
                clean_snippet = (
                    sanitize_text(item.content.strip()[:80], allow_empty=True).cleaned_text
                    or clean_title
                )
                is_gold_venting = extract_out.is_venting_or_irony and target_tier is Tier.GOLD
                rec_state = (
                    RecordReviewState.HELD.value
                    if is_gold_venting
                    else RecordReviewState.NORMAL.value
                )
                rec_row_existing = RecordRow(
                    id=rec_id,
                    user_id=scope.user_id,
                    person_id=target_pid,
                    capture_id=capture_id,
                    kind=RecordKind.NOTE.value,
                    title_enc=container.cipher.encrypt_str(
                        dek,
                        clean_title,
                        user_id=scope.user_id,
                        table="records",
                        column="title_enc",
                        row_id=rec_id,
                    ),
                    body_enc=container.cipher.encrypt_str(
                        dek,
                        clean_body,
                        user_id=scope.user_id,
                        table="records",
                        column="body_enc",
                        row_id=rec_id,
                    ),
                    snippet_enc=container.cipher.encrypt_str(
                        dek,
                        clean_snippet,
                        user_id=scope.user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=rec_id,
                    ),
                    happened_on=local_today,
                    pinned=True,
                    review_state=rec_state,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(rec_row_existing)
                await session.flush()
                records_by_person[target_pid] = rec_row_existing
                record_ids.append(rec_id)

            clean_fact_content = (
                sanitize_text(item.content, allow_empty=True).cleaned_text or item.content.strip()
            )
            raw_fact_title = item.title.strip() or clean_fact_content[:24] or "手帐便签"
            clean_fact_title = (
                sanitize_text(raw_fact_title, allow_empty=True).cleaned_text or "手帐便签"
            )

            candidate_count = (
                2 if extract_out.needs_disambiguation and len(cands) == 1 else len(cands)
            )
            is_polarity_flip = False
            if len(cands) == 1:
                is_polarity_flip = await check_polarity_flip_for_person(
                    session,
                    scope=scope.user_scope,
                    person_id=target_pid,
                    fact_type=item.fact_type,
                    polarity=item.polarity,
                    content=clean_fact_content,
                    dek=dek,
                    cipher=container.cipher,
                )

            fact_status, pending_reason = decide_chat_writeback_gate(
                fact_type=item.fact_type,
                confidence=item.confidence,
                candidate_count=candidate_count,
                multi_person=multi_person,
                scoped_locked=is_locked_to_current,
                is_venting_or_irony=extract_out.is_venting_or_irony,
                tier=target_tier,
                person_state=target_state,
                source_speaker=item.source_speaker,
                polarity_flipped=is_polarity_flip,
                extraction_ok=True,
                confidence_threshold=container.settings.gate_confidence_threshold,
            )

            fid = new_id()
            frow = FactRow(
                id=fid,
                user_id=scope.user_id,
                person_id=target_pid if len(cands) == 1 else None,
                record_id=rec_row_existing.id,
                capture_id=capture_id,
                type=item.fact_type.value,
                polarity=item.polarity.value,
                time_status=item.time_status.value,
                source_speaker=item.source_speaker.value,
                content_enc=container.cipher.encrypt_str(
                    dek,
                    clean_fact_content,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fid,
                ),
                trigger_date=_parse_iso_date(item.trigger_date),
                is_lunar=item.is_lunar,
                lunar_month=item.lunar_month,
                lunar_day=item.lunar_day,
                lunar_is_leap_month=False,
                status=fact_status.value,
                pending_reason=pending_reason.value if pending_reason is not None else None,
                confidence=item.confidence,
                created_by=CreatedBy.AI.value,
                valid_at=now,
                prompt_version=prompt_ver,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(frow)
            await session.flush()

            if fact_status is FactStatus.CONFIRMED:
                confirmed_fact_ids.append(fid)
                await apply_bitemporal_supersession_for_fact(
                    session,
                    scope=scope.user_scope,
                    dek=dek,
                    cipher=container.cipher,
                    new_fact=frow,
                    new_fact_content=clean_fact_content,
                    now=now,
                )
                if item.fact_type in (FactType.DATE, FactType.PROMISE):
                    await upsert_reminder_for_fact(
                        session,
                        scope=scope.user_scope,
                        dek=dek,
                        cipher=container.cipher,
                        fact=frow,
                        fact_content=clean_fact_content,
                        desired_status=ReminderStatus.ACTIVE,
                        now=now,
                        tz_name=tz_name,
                    )
            else:
                pending_fact_ids.append(fid)
                if (
                    item.fact_type in (FactType.DATE, FactType.PROMISE)
                    and frow.person_id is not None
                ):
                    await upsert_reminder_for_fact(
                        session,
                        scope=scope.user_scope,
                        dek=dek,
                        cipher=container.cipher,
                        fact=frow,
                        fact_content=clean_fact_content,
                        desired_status=ReminderStatus.PROPOSED,
                        now=now,
                        tz_name=tz_name,
                    )

                if pending_reason is PendingReason.VENTING:
                    ikind = InboxItemKind.VENTING_HOLD
                    expires_at = now + timedelta(hours=container.settings.venting_hold_expiry_hours)
                elif pending_reason is PendingReason.AMBIGUOUS_PERSON:
                    ikind = InboxItemKind.PERSON_DISAMBIGUATION
                    expires_at = None
                elif pending_reason is PendingReason.NEW_PERSON:
                    ikind = InboxItemKind.NEW_PERSON_CONFIRM
                    expires_at = None
                else:
                    ikind = InboxItemKind.FACT_CONFIRM
                    expires_at = None

                if clean_fact_title and clean_fact_title not in clean_fact_content:
                    summary_text = f"{clean_fact_title}：{clean_fact_content}"
                else:
                    summary_text = clean_fact_content or clean_fact_title or "待确认手帐要点"
                target_person_name = person_names_by_id.get(target_pid, "")

                iid = new_id()
                irow = InboxItemRow(
                    id=iid,
                    user_id=scope.user_id,
                    person_id=target_pid if len(cands) == 1 else None,
                    capture_id=capture_id,
                    record_id=rec_row_existing.id,
                    kind=ikind.value,
                    status=InboxItemStatus.OPEN.value,
                    fact_ids=[fid],
                    candidate_person_ids=cands,
                    payload_enc=container.cipher.encrypt_json(
                        dek,
                        {
                            "summary": summary_text,
                            "title": clean_fact_title,
                            "content": clean_fact_content,
                            "factType": item.fact_type.value,
                            "personName": target_person_name,
                            "mention": item.person_mention.strip() or target_person_name,
                            "pendingReason": (
                                pending_reason.value if pending_reason is not None else "review"
                            ),
                        },
                        user_id=scope.user_id,
                        table="inbox_items",
                        column="payload_enc",
                        row_id=iid,
                    ),
                    expires_at=expires_at,
                    created_at=now,
                )
                session.add(irow)
                inbox_item_ids.append(iid)

        capture_row.status = (
            CaptureStatus.NEEDS_REVIEW.value if pending_fact_ids else CaptureStatus.COMPLETED.value
        )
        capture_row.undo_deadline = (
            now + timedelta(seconds=container.settings.undo_window_seconds)
            if confirmed_fact_ids and not pending_fact_ids
            else None
        )
        capture_row.completed_at = now
        await session.flush()

        return WritebackResult(
            capture_id=capture_id,
            record_ids=record_ids,
            confirmed_fact_ids=confirmed_fact_ids,
            pending_fact_ids=pending_fact_ids,
            inbox_item_ids=inbox_item_ids,
            status=capture_row.status,
        )


async def summarize_chat_thread(
    container: AppContainer,
    *,
    scope: PersonScope,
    model_gateway: ModelGatewayPort | None = None,
) -> str | None:
    """Update `ChatThreadRow.rolling_summary_enc` from recent conversation turns."""
    gateway = _resolve_gateway(container, model_gateway)

    async with container.db.user_session(scope.user_scope) as session:
        person_row = await session.scalar(
            select(PersonRow).where(
                PersonRow.user_id == scope.user_id,
                PersonRow.id == scope.person_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
            )
        )
        if person_row is None:
            raise NotFoundError("Person not found for thread summarization")

        thread = await session.scalar(
            select(ChatThreadRow)
            .where(
                ChatThreadRow.user_id == scope.user_id,
                ChatThreadRow.person_id == scope.person_id,
            )
            .with_for_update()
        )
        if thread is None:
            return None

        dek = await container.load_user_dek(session, scope.user_scope)
        person_name = container.cipher.decrypt_str(
            dek,
            person_row.display_name_enc,
            user_id=scope.user_id,
            table="persons",
            column="display_name_enc",
            row_id=person_row.id,
        )

        prev_summary: str | None = None
        if thread.rolling_summary_enc is not None:
            prev_summary = container.cipher.decrypt_str(
                dek,
                thread.rolling_summary_enc,
                user_id=scope.user_id,
                table="chat_threads",
                column="rolling_summary_enc",
                row_id=thread.id,
            )

        recent_rows = list(
            (
                await session.scalars(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.user_id == scope.user_id,
                        ChatMessageRow.person_id == scope.person_id,
                    )
                    .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
                    .limit(12)
                )
            ).all()
        )
        if not recent_rows:
            return prev_summary

        recent_rows.reverse()
        lines: list[str] = []
        for r in recent_rows:
            txt = container.cipher.decrypt_str(
                dek,
                r.content_enc,
                user_id=scope.user_id,
                table="chat_messages",
                column="content_enc",
                row_id=r.id,
            )
            lines.append(f"{r.role}: {mask_hard_pii(txt).masked_text}")

        new_summary_raw = await gateway.summarize_thread(
            scope=scope.user_scope,
            person_name=mask_hard_pii(person_name).masked_text,
            previous_summary=mask_hard_pii(prev_summary).masked_text if prev_summary else None,
            turns_text="\n".join(lines),
            session=session,
        )
        cleaned_summary = sanitize_text(new_summary_raw, allow_empty=True).cleaned_text
        if not cleaned_summary:
            return prev_summary

        thread.rolling_summary_enc = container.cipher.encrypt_str(
            dek,
            cleaned_summary,
            user_id=scope.user_id,
            table="chat_threads",
            column="rolling_summary_enc",
            row_id=thread.id,
        )
        thread.updated_at = container.clock.now()
        await session.flush()
        return cleaned_summary


def _extract_job_mapping(job_or_payload: Any) -> dict[str, Any]:
    if isinstance(job_or_payload, dict):
        return job_or_payload
    payload_attr = getattr(job_or_payload, "payload", None)
    if isinstance(payload_attr, dict):
        return payload_attr
    return dict(job_or_payload)


async def handle_writeback_chat_facts_job(
    container: AppContainer,
    payload: Any,
    *,
    model_gateway: ModelGatewayPort | None = None,
) -> WritebackResult:
    """Worker entrypoint for `TASK_WRITEBACK_CHAT_FACTS` (accepts `dict` or `ClaimedJob`)."""
    data = _extract_job_mapping(payload)
    scope = PersonScope(
        user_id=UUID(str(data["user_id"])),
        person_id=UUID(str(data["person_id"])),
    )
    message_id = UUID(str(data["message_id"]))
    return await writeback_facts_from_chat_message(
        container,
        scope=scope,
        message_id=message_id,
        model_gateway=model_gateway,
    )


async def handle_summarize_thread_job(
    container: AppContainer,
    payload: Any,
    *,
    model_gateway: ModelGatewayPort | None = None,
) -> str | None:
    """Worker entrypoint for `TASK_SUMMARIZE_THREAD` (accepts `dict` or `ClaimedJob`)."""
    data = _extract_job_mapping(payload)
    scope = PersonScope(
        user_id=UUID(str(data["user_id"])),
        person_id=UUID(str(data["person_id"])),
    )
    return await summarize_chat_thread(
        container,
        scope=scope,
        model_gateway=model_gateway,
    )


def register_companion_worker_handlers(
    runner: Any,
    container: AppContainer,
    *,
    model_gateway: ModelGatewayPort | None = None,
) -> None:
    """Register `TASK_WRITEBACK_CHAT_FACTS` and `TASK_SUMMARIZE_THREAD` handlers on `WorkerRunner`."""

    async def _on_writeback(job: Any) -> WritebackResult:
        return await handle_writeback_chat_facts_job(container, job, model_gateway=model_gateway)

    async def _on_summarize(job: Any) -> str | None:
        return await handle_summarize_thread_job(container, job, model_gateway=model_gateway)

    runner.register_handler(TASK_WRITEBACK_CHAT_FACTS, _on_writeback)
    runner.register_handler(TASK_SUMMARIZE_THREAD, _on_summarize)
