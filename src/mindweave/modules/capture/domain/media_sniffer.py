"""Pure binary magic-byte sniffing and size validation for uploaded audio."""

from __future__ import annotations

from dataclasses import dataclass


class MediaValidationError(ValueError):
    """Base domain error for invalid or oversized media uploads."""


class MediaTooLargeError(MediaValidationError):
    """Uploaded media exceeds configured byte limit."""


class InvalidMediaFormatError(MediaValidationError):
    """Uploaded media fails binary magic-byte signature verification."""


@dataclass(frozen=True, slots=True)
class AudioFormatInfo:
    mime_type: str
    extension: str
    byte_length: int


def sniff_audio_magic(data: bytes) -> tuple[str, str]:
    """Verify binary magic bytes for `.webm`, `.wav`, `.m4a`/MP4, `.ogg`, and `.mp3`.

    Returns `(mime_type, extension)` or raises `InvalidMediaFormatError`.
    """
    if len(data) < 4:
        raise InvalidMediaFormatError("Audio payload is empty or truncated.")

    # 1. WebM / Matroska EBML header: 1A 45 DF A3
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", "webm"

    # 2. WAV: RIFF....WAVE
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"

    # 3. M4A / MP4 ISO Base Media File Format: ....ftyp
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"

    # 4. Ogg container: OggS
    if data[:4] == b"OggS":
        return "audio/ogg", "ogg"

    # 5. MP3: ID3v2 tag ("ID3") or MPEG audio frame sync (FF FB / FF F3 / FF F2 / FF FA, etc.)
    if data[:3] == b"ID3":
        return "audio/mpeg", "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x18) != 0x08:
        return "audio/mpeg", "mp3"

    raise InvalidMediaFormatError(
        "Unsupported or corrupted audio format; expected webm, wav, m4a, ogg, or mp3."
    )


def validate_and_sniff_audio(data: bytes, *, max_bytes: int) -> AudioFormatInfo:
    """Enforce size limit and binary magic-byte verification for audio uploads."""
    if not data:
        raise InvalidMediaFormatError("Audio payload cannot be empty.")
    if len(data) > max_bytes:
        raise MediaTooLargeError(
            f"Audio file size ({len(data)} bytes) exceeds maximum allowed ({max_bytes} bytes)."
        )
    mime_type, ext = sniff_audio_magic(data)
    return AudioFormatInfo(mime_type=mime_type, extension=ext, byte_length=len(data))
