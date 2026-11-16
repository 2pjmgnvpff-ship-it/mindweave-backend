"""ORM → pure D1 pencil projections shared by capture splits and the person page."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from mindweave.core.orm import FactRow
from mindweave.shared.enums import FactStatus, FactType, InboxItemKind, PendingReason
from mindweave.shared.pencil import PencilFactView

# The inbox item a pencil/question slip should resolve through, most actionable first.
_PRIMARY_INBOX_ORDER: tuple[InboxItemKind, ...] = (
    InboxItemKind.NEW_PERSON_CONFIRM,
    InboxItemKind.PERSON_DISAMBIGUATION,
    InboxItemKind.FACT_CONFIRM,
    InboxItemKind.VENTING_HOLD,
)


def pending_reason_or_none(raw: str | None) -> PendingReason | None:
    if not raw:
        return None
    try:
        return PendingReason(raw)
    except ValueError:
        return None


def fact_row_to_pencil_view(row: FactRow) -> PencilFactView:
    try:
        fact_type = FactType(row.type)
    except ValueError:
        fact_type = FactType.PREFERENCE
    try:
        status = FactStatus(row.status)
    except ValueError:
        status = FactStatus.CONFIRMED
    return PencilFactView(
        fact_type=fact_type,
        status=status,
        pending_reason=pending_reason_or_none(row.pending_reason),
        needs_glance=bool(row.needs_glance),
        trigger_date=row.trigger_date,
        is_lunar=bool(row.is_lunar),
        lunar_month=row.lunar_month,
        lunar_day=row.lunar_day,
    )


def primary_inbox_id(by_kind: Mapping[InboxItemKind, UUID]) -> UUID | None:
    for kind in _PRIMARY_INBOX_ORDER:
        if kind in by_kind:
            return by_kind[kind]
    return next(iter(by_kind.values()), None)
