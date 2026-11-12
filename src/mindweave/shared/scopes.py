"""Tenant and person isolation scope value objects (Invariant I1)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UserScope:
    """Mandatory scope token proving tenant context has been bound."""

    user_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, UUID) or self.user_id.int == 0:
            raise ValueError("UserScope requires a valid non-nil UUID")

    def person(self, person_id: UUID) -> PersonScope:
        return PersonScope(user_id=self.user_id, person_id=person_id)


@dataclass(frozen=True, slots=True)
class PersonScope:
    """Mandatory scope token for per-person memory/chat retrieval (Invariant I1)."""

    user_id: UUID
    person_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, UUID) or self.user_id.int == 0:
            raise ValueError("PersonScope requires a valid non-nil user_id")
        if not isinstance(self.person_id, UUID) or self.person_id.int == 0:
            raise ValueError("PersonScope requires a valid non-nil person_id")

    @property
    def user_scope(self) -> UserScope:
        return UserScope(user_id=self.user_id)
