"""Privacy-preserving compliance audit log service (PIPL Art. 56)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.ids import new_id
from mindweave.core.logging import sanitize_log_value
from mindweave.core.orm import AuditLogRow


def anonymize_actor_id(user_id: UUID, *, secret: str) -> str:
    """Compute irreversible HMAC-SHA256 actor hash so audit tombstones outlive account deletion."""
    return hmac.new(
        secret.encode("utf-8"),
        f"audit-actor:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def anonymize_ip(client_ip: str | None, *, secret: str) -> str | None:
    if not client_ip:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        f"audit-ip:{client_ip}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


class AuditService:
    """Appends sanitized, zero-PII compliance audit events via `system_session`."""

    def __init__(self, *, clock: Clock, secret: str) -> None:
        self._clock = clock
        self._secret = secret

    async def record(
        self,
        system_session: AsyncSession,
        *,
        user_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> AuditLogRow:
        safe_meta = sanitize_log_value(metadata or {})
        if not isinstance(safe_meta, dict):
            safe_meta = {}
        row = AuditLogRow(
            id=new_id(),
            actor_user_hash=anonymize_actor_id(user_id, secret=self._secret),
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=safe_meta,
            ip_hash=anonymize_ip(client_ip, secret=self._secret),
            created_at=self._clock.now(),
        )
        system_session.add(row)
        await system_session.flush()
        return row
