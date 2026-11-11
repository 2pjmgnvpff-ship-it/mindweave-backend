"""Registry of user-scoped tables for export & deletion completeness (Invariant I8)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DataTableRegistration:
    table_name: str
    owning_module: str
    export_section: (
        str | None
    )  # None ONLY if transient/ephemeral security metadata (e.g. idempotency_keys)
    deletion_strategy: str  # "cascade_from_users" | "explicit_delete" | "crypto_shred"
    rationale: str = ""


class DataRightsRegistry:
    """Global registry verified by CI architecture tests against SQLAlchemy metadata."""

    def __init__(self) -> None:
        self._tables: dict[str, DataTableRegistration] = {}

    def register(self, registration: DataTableRegistration) -> None:
        existing = self._tables.get(registration.table_name)
        if existing is not None and existing != registration:
            raise ValueError(
                f"Conflicting DataRightsRegistry entry for {registration.table_name}: "
                f"{existing} vs {registration}"
            )
        self._tables[registration.table_name] = registration

    def get(self, table_name: str) -> DataTableRegistration | None:
        return self._tables.get(table_name)

    def all_registrations(self) -> dict[str, DataTableRegistration]:
        return dict(self._tables)


data_rights_registry = DataRightsRegistry()
