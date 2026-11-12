"""Push notification provider ports and APNs / Web Push / in-memory test adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mindweave.core.ids import new_id
from mindweave.core.logging import get_logger
from mindweave.modules.reminders.domain.notification_copy import validate_push_copy_safety
from mindweave.shared.enums import PushPlatform

logger = get_logger(__name__)


class PushDeliveryError(RuntimeError):
    """Raised when an outbound push notification adapter fails transiently or permanently."""


@dataclass(frozen=True, slots=True)
class PushMessage:
    """Outbound privacy-desensitized push envelope dispatched to a single registered device."""

    device_id: UUID
    user_id: UUID
    reminder_id: UUID
    platform: PushPlatform
    push_token: str
    title: str
    body: str
    deeplink_path: str
    scheduled_for: datetime
    lead_day: int


@dataclass(frozen=True, slots=True)
class PushDeliveryReceipt:
    """Provider acknowledgement for an outbound push notification."""

    delivered: bool
    channel: str
    provider_message_id: str | None = None
    failure_reason: str | None = None


class PushProviderPort(Protocol):
    async def send(self, message: PushMessage) -> PushDeliveryReceipt: ...


class APNsLogAdapter:
    """Structured zero-PII APNs log adapter for local/dev/staging until live APNs credentials."""

    async def send(self, message: PushMessage) -> PushDeliveryReceipt:
        validate_push_copy_safety(message.title, message.body)
        msg_id = f"apns-log-{new_id()}"
        # Never log push_token or any user PII (Invariant I10)!
        logger.info(
            "push.apns.dispatched",
            provider_message_id=msg_id,
            device_id=str(message.device_id),
            reminder_id=str(message.reminder_id),
            lead_day=message.lead_day,
        )
        return PushDeliveryReceipt(
            delivered=True,
            channel=PushPlatform.IOS_APNS.value,
            provider_message_id=msg_id,
        )


class WebPushLogAdapter:
    """Structured zero-PII Web Push VAPID log adapter for local/dev/staging."""

    async def send(self, message: PushMessage) -> PushDeliveryReceipt:
        validate_push_copy_safety(message.title, message.body)
        msg_id = f"webpush-log-{new_id()}"
        logger.info(
            "push.web_push.dispatched",
            provider_message_id=msg_id,
            device_id=str(message.device_id),
            reminder_id=str(message.reminder_id),
            lead_day=message.lead_day,
        )
        return PushDeliveryReceipt(
            delivered=True,
            channel=PushPlatform.WEB_PUSH.value,
            provider_message_id=msg_id,
        )


class CompositePushDispatcher:
    """Routes outbound push messages to platform-specific `PushProviderPort` adapters."""

    def __init__(
        self,
        *,
        apns: PushProviderPort | None = None,
        web_push: PushProviderPort | None = None,
    ) -> None:
        self._apns: PushProviderPort = apns or APNsLogAdapter()
        self._web_push: PushProviderPort = web_push or WebPushLogAdapter()

    async def send(self, message: PushMessage) -> PushDeliveryReceipt:
        if message.platform is PushPlatform.IOS_APNS:
            return await self._apns.send(message)
        if message.platform is PushPlatform.WEB_PUSH:
            return await self._web_push.send(message)
        raise PushDeliveryError(f"Unsupported push platform {message.platform!r}")


class InMemoryPushRecorder:
    """Deterministic in-memory push capture adapter for unit and integration tests."""

    def __init__(self, *, fail_next: int = 0) -> None:
        self.messages: list[PushMessage] = []
        self._fail_remaining = fail_next

    @property
    def deliveries(self) -> list[PushMessage]:
        return self.messages

    def set_fail_next(self, count: int) -> None:
        self._fail_remaining = max(0, count)

    def clear(self) -> None:
        self.messages.clear()
        self._fail_remaining = 0

    async def send(self, message: PushMessage) -> PushDeliveryReceipt:
        validate_push_copy_safety(message.title, message.body)
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise PushDeliveryError("Simulated transient push provider failure")
        self.messages.append(message)
        return PushDeliveryReceipt(
            delivered=True,
            channel=message.platform.value,
            provider_message_id=f"mem-push-{len(self.messages)}",
        )
