"""Pillow image verification, orientation normalization, and complete EXIF stripping."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from mindweave.core.errors import PayloadTooLargeError, ValidationFailedError

MAX_IMAGE_PIXELS = 36_000_000  # 6000x6000 safety ceiling against decompression bombs


@dataclass(frozen=True, slots=True)
class ProcessedPhoto:
    """Verified, EXIF-free encoded image ready for per-user envelope encryption."""

    data: bytes
    content_type: str
    extension: str
    width: int
    height: int


def _process_image_sync(raw_bytes: bytes, *, max_bytes: int) -> ProcessedPhoto:
    if not raw_bytes:
        raise ValidationFailedError("Uploaded image file must not be empty.")
    if len(raw_bytes) > max_bytes:
        raise PayloadTooLargeError(f"Uploaded image exceeds maximum limit of {max_bytes} bytes.")

    try:
        with Image.open(BytesIO(raw_bytes)) as probe:
            detected_format = (probe.format or "").upper()
            width, height = probe.size
            if width < 1 or height < 1 or (width * height) > MAX_IMAGE_PIXELS:
                raise ValidationFailedError("Image dimensions are invalid or exceed safety bounds.")
            probe.verify()
    except (ValidationFailedError, PayloadTooLargeError):
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ValidationFailedError("Uploaded file is not a valid image.") from exc

    if detected_format not in ("JPEG", "PNG", "WEBP"):
        raise ValidationFailedError(
            "Unsupported image format; only JPEG, PNG, and WebP are accepted."
        )

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            oriented = ImageOps.exif_transpose(img)
            final_w, final_h = oriented.size
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info

            out = BytesIO()
            if detected_format == "JPEG":
                rgb = oriented.convert("RGB")
                # Reconstruct from raw pixel buffer so zero EXIF/GPS/XMP/ICC metadata survives
                clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
                clean.save(out, format="JPEG", quality=90, optimize=True)
                content_type = "image/jpeg"
                ext = "jpg"
            elif detected_format == "WEBP":
                target_mode = "RGBA" if has_alpha else "RGB"
                converted = oriented.convert(target_mode)
                clean = Image.frombytes(target_mode, converted.size, converted.tobytes())
                clean.save(out, format="WEBP", quality=90)
                content_type = "image/webp"
                ext = "webp"
            else:
                target_mode = "RGBA" if has_alpha else "RGB"
                converted = oriented.convert(target_mode)
                clean = Image.frombytes(target_mode, converted.size, converted.tobytes())
                clean.save(out, format="PNG", optimize=True)
                content_type = "image/png"
                ext = "png"

            return ProcessedPhoto(
                data=out.getvalue(),
                content_type=content_type,
                extension=ext,
                width=final_w,
                height=final_h,
            )
    except (ValidationFailedError, PayloadTooLargeError):
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ValidationFailedError("Failed to decode image payload.") from exc


async def verify_and_strip_exif(raw_bytes: bytes, *, max_bytes: int) -> ProcessedPhoto:
    """Verify image integrity and strip all EXIF/metadata in a worker thread."""
    return await asyncio.to_thread(_process_image_sync, raw_bytes, max_bytes=max_bytes)
