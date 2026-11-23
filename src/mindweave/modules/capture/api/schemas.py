"""CamelCase API schemas for conversational voice-only ASR (`POST /v1/voice/transcribe`)."""

from __future__ import annotations

from mindweave.core.dto import CamelModel


class VoiceTranscribeResponse(CamelModel):
    """Pure conversational ASR response (zero `CaptureRow`, `RecordRow`, or `FactRow` mutations)."""

    transcript: str
    corrected: str
    raw_transcript: str = ""
    duration_ms: int | None = None
