"""Port protocols for AI Model Gateway & Safety Subsystem."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    ExtractRecordInput,
    ExtractRecordOutput,
    SafetyClassifyOutput,
    SafetyDecision,
    ScreenshotParseOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.shared.scopes import UserScope


class ModelGatewayPort(Protocol):
    async def transcribe(
        self,
        *,
        scope: UserScope,
        payload: TranscribeInput,
        session: AsyncSession | None = None,
    ) -> TranscribeOutput: ...

    async def parse_screenshot(
        self,
        *,
        scope: UserScope,
        image_bytes: bytes,
        mime_type: str,
        target_person_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> ScreenshotParseOutput: ...

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        """Return `(result, prompt_version, model_profile)`."""
        ...

    async def generate_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> tuple[CompanionChatOutput, str, str]:
        """Return `(result, prompt_version, model_profile)`."""
        ...

    def stream_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        """Yield incremental text chunks of the main reply."""
        ...

    async def classify_safety(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyClassifyOutput: ...

    async def summarize_thread(
        self,
        *,
        scope: UserScope,
        person_name: str,
        previous_summary: str | None,
        turns_text: str,
        session: AsyncSession | None = None,
    ) -> str: ...


class SafetyServicePort(Protocol):
    async def evaluate(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyDecision: ...
