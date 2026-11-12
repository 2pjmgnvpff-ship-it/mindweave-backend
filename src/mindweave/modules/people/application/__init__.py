"""Application layer exports for the People module."""

from mindweave.modules.people.application.entity_resolver import (
    EntityResolutionResult,
    EntityResolver,
    ProvisionedPerson,
    create_provisional_person,
    list_known_persons,
)
from mindweave.modules.people.application.service import (
    ListedPersonsResult,
    MoveOutcome,
    PeopleService,
    TierTransitionOutcome,
)

__all__ = [
    "EntityResolutionResult",
    "EntityResolver",
    "ListedPersonsResult",
    "MoveOutcome",
    "PeopleService",
    "ProvisionedPerson",
    "TierTransitionOutcome",
    "create_provisional_person",
    "list_known_persons",
]
