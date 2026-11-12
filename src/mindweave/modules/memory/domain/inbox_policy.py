"""Pure domain validation rules for Pending Inbox resolution actions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from mindweave.shared.enums import InboxItemKind, InboxItemStatus, InboxResolutionAction


class InboxDomainError(ValueError):
    """Domain violation during inbox item resolution."""

    def __init__(self, message: str, *, code: str = "invalid_inbox_resolution") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InboxResolutionInput:
    item_kind: InboxItemKind
    item_status: InboxItemStatus
    action: InboxResolutionAction
    current_person_id: UUID | None = None
    target_person_id: UUID | None = None
    display_name: str | None = None
    fallback_mention: str | None = None


def validate_inbox_resolution(inp: InboxResolutionInput) -> None:
    """Validate inbox resolution state transition and required action arguments."""
    if inp.item_status is not InboxItemStatus.OPEN:
        raise InboxDomainError(
            f"Inbox item is already {inp.item_status.value} and cannot be resolved again.",
            code="inbox_not_open",
        )

    if inp.action is InboxResolutionAction.CONFIRM:
        if inp.current_person_id is None and inp.target_person_id is None:
            raise InboxDomainError(
                "Ambiguous inbox item requires targetPersonId via assign_person or create_person.",
                code="missing_person_for_confirm",
            )
    elif inp.action is InboxResolutionAction.ASSIGN_PERSON:
        if inp.target_person_id is None:
            raise InboxDomainError(
                "targetPersonId is required when action is 'assign_person'.",
                code="missing_target_person_id",
            )
    elif inp.action is InboxResolutionAction.MERGE_PERSON:
        if inp.target_person_id is None:
            raise InboxDomainError(
                "targetPersonId is required when action is 'merge_person'.",
                code="missing_target_person_id",
            )
        if inp.current_person_id is None:
            raise InboxDomainError(
                "merge_person requires a provisional source person on the inbox item.",
                code="missing_source_person_for_merge",
            )
        if inp.current_person_id == inp.target_person_id:
            raise InboxDomainError(
                "Cannot merge a person into itself.",
                code="self_merge_forbidden",
            )
    elif inp.action is InboxResolutionAction.CREATE_PERSON:
        if inp.current_person_id is None:
            effective_name = (inp.display_name or inp.fallback_mention or "").strip()
            if not effective_name:
                raise InboxDomainError(
                    "displayName is required to create a new person when no mention was captured.",
                    code="missing_display_name",
                )
