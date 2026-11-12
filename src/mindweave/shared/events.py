"""Lightweight typed in-process domain event bus."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID


@dataclass(frozen=True)
class DomainEvent:
    event_id: UUID
    occurred_at: datetime
    user_id: UUID


E = TypeVar("E", bound=DomainEvent)
EventHandler = Callable[[Any], Awaitable[None]]


class EventBus:
    """In-process async domain event dispatcher."""

    def __init__(self) -> None:
        self._subscribers: dict[type[DomainEvent], list[EventHandler]] = defaultdict(list)

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
    ) -> None:
        self._subscribers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        for handler in list(self._subscribers.get(type(event), ())):
            await handler(event)

    def clear(self) -> None:
        self._subscribers.clear()
