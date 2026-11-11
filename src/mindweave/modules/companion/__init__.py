"""MindWeave Cupid Companion Chat module."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindweave.modules.companion.application import (
        CompanionService,
        WritebackResult,
        handle_summarize_thread_job,
        handle_writeback_chat_facts_job,
        register_companion_worker_handlers,
        summarize_chat_thread,
        writeback_facts_from_chat_message,
    )

__all__ = [
    "CompanionService",
    "WritebackResult",
    "handle_summarize_thread_job",
    "handle_writeback_chat_facts_job",
    "register_companion_worker_handlers",
    "summarize_chat_thread",
    "writeback_facts_from_chat_message",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from mindweave.modules.companion import application as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
