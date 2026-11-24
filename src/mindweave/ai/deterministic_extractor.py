"""First-class deterministic local Chinese extraction fallback.

Used ONLY when all configured cloud LLM providers fail (network outage / 429 /
timeout) or in offline test mode. Operates strictly on real user input / real
on-device `client_transcript` (NEVER fabricates speech!) and transparently flags
`needs_cloud_refinement=True` so callers know extraction ran locally.
"""

from __future__ import annotations

from datetime import date

from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.schemas.contracts import ExtractRecordInput, ExtractRecordOutput
from mindweave.ai.validators.asr_postprocess import postprocess_asr_transcript
from mindweave.shared.enums import FactType

_EXTRACTOR_ADAPTER = FakeAdapter()


def _safe_parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def extract_deterministically(payload: ExtractRecordInput) -> ExtractRecordOutput:
    """Run deterministic Chinese person attribution, multi-event splitting & date parsing.

    Guarantees:
    - Applies conservative Pinyin/homophone roster snap + contrast-safe self-correction
      before extraction (`repaired_transcript`).
    - Populates `source_span` and enforces `happened_at <= current_date` (never
      backdating future reminders or recurring birthdays into `happened_at`).
    - Marks `needs_cloud_refinement=True` so API consumers can distinguish local
      fallback from cloud LLM extraction honestly.
    """
    known_names = [
        nm
        for person in payload.known_persons
        for nm in (person.display_name, *person.aliases)
        if nm and nm.strip()
    ]
    scoped_name: str | None = None
    if payload.scoped_person_id is not None:
        for person in payload.known_persons:
            if person.person_id == payload.scoped_person_id:
                scoped_name = person.display_name
                break

    cleaned_text = postprocess_asr_transcript(
        payload.text,
        active_person_name=scoped_name,
        known_person_names=known_names,
        recent_context_snippets=payload.recent_history_hints,
    )
    effective_text = cleaned_text or payload.text
    effective_payload = payload.model_copy(update={"text": effective_text})

    raw_out = _EXTRACTOR_ADAPTER._handle_extract_record(
        {"payload": effective_payload.model_dump(mode="json")}
    )

    ref_date = _safe_parse_date(payload.current_date)
    normalized_facts = []
    for fact in raw_out.facts:
        happened_at = fact.happened_at
        if happened_at is None and ref_date is not None and fact.fact_type not in (
            FactType.DATE,
            FactType.PREFERENCE,
            FactType.TABOO,
            FactType.WISH,
        ):
            trig = _safe_parse_date(fact.trigger_date)
            if trig is not None and trig <= ref_date:
                happened_at = trig.isoformat()
        elif happened_at is not None and ref_date is not None:
            h_dt = _safe_parse_date(happened_at)
            if h_dt is None or h_dt > ref_date:
                happened_at = None

        normalized_facts.append(
            fact.model_copy(
                update={
                    "happened_at": happened_at,
                    "source_span": fact.source_span or fact.content,
                }
            )
        )

    return raw_out.model_copy(
        update={
            "facts": normalized_facts,
            "repaired_transcript": (
                effective_text if effective_text != payload.text.strip() else None
            ),
            "needs_cloud_refinement": True,
        }
    )
