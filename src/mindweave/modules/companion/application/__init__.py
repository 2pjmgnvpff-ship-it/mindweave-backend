"""Public application layer exports for the Companion module."""

from mindweave.modules.companion.application.dto import (
    ChatCitationDto,
    ChatMessageDto,
    ChatMessagePage,
    ChatTurnResponse,
    ClearChatHistoryResponse,
    CrisisCardDto,
    SendChatMessageRequest,
    StreamChatRequest,
)
from mindweave.modules.companion.application.safety_fallback import BuiltinSafetyFallback
from mindweave.modules.companion.application.service import (
    CompanionService,
    format_sse_event,
)
from mindweave.modules.companion.application.writeback import (
    WritebackResult,
    handle_summarize_thread_job,
    handle_writeback_chat_facts_job,
    register_companion_worker_handlers,
    summarize_chat_thread,
    writeback_facts_from_chat_message,
)

__all__ = [
    "BuiltinSafetyFallback",
    "ChatCitationDto",
    "ChatMessageDto",
    "ChatMessagePage",
    "ChatTurnResponse",
    "ClearChatHistoryResponse",
    "CompanionService",
    "CrisisCardDto",
    "SendChatMessageRequest",
    "StreamChatRequest",
    "WritebackResult",
    "format_sse_event",
    "handle_summarize_thread_job",
    "handle_writeback_chat_facts_job",
    "register_companion_worker_handlers",
    "summarize_chat_thread",
    "writeback_facts_from_chat_message",
]
