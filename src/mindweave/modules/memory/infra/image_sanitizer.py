"""Pillow-based image validation and complete EXIF/metadata stripper (PIA Shield 2)."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from mindweave.core.errors import PayloadTooLargeError, ValidationFailedError


@dataclass(frozen=True, slots=True)
class SanitizedImage:
    clean_bytes: bytes
    mime_type: str
    extension: str
    width: int
    height: int


_ALLOWED_FORMATS: dict[str, tuple[str, str, str]] = {
    "JPEG": ("JPEG", "image/jpeg", "jpg"),
    "PNG": ("PNG", "image/png", "png"),
    "WEBP": ("WEBP", "image/webp", "webp"),
}


def validate_and_strip_image_exif(data: bytes, *, max_bytes: int) -> SanitizedImage:
    """Validate image bytes via Pillow and re-encode a clean pixel buffer with zero EXIF."""
    if not data:
        raise ValidationFailedError("Uploaded image payload cannot be empty.")
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"Image file size ({len(data)} bytes) exceeds maximum allowed ({max_bytes} bytes)."
        )

    try:
        with Image.open(BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValidationFailedError("Invalid or corrupted image file.") from exc

    if fmt not in _ALLOWED_FORMATS:
        # Fallback convert uncommon valid raster formats to PNG, or reject non-raster
        save_fmt, mime_type, ext = ("PNG", "image/png", "png")
    else:
        save_fmt, mime_type, ext = _ALLOWED_FORMATS[fmt]

    try:
        with Image.open(BytesIO(data)) as raw_img:
            transposed = ImageOps.exif_transpose(raw_img)
            width, height = transposed.size
            if width <= 0 or height <= 0 or width > 12000 or height > 12000:
                raise ValidationFailedError("Image dimensions are out of bounds.")

            target_mode = transposed.mode
            if save_fmt == "JPEG" and target_mode not in ("RGB", "L"):
                transposed = transposed.convert("RGB")
                target_mode = "RGB"
            elif target_mode not in ("RGB", "RGBA", "L"):
                transposed = transposed.convert("RGBA")
                target_mode = "RGBA"

            # Copy raw pixel buffer into a brand-new Image instance so zero EXIF/XMP/comment survives
            clean_img = Image.frombytes(target_mode, transposed.size, transposed.tobytes())
            out = BytesIO()
            if save_fmt == "JPEG":
                clean_img.save(out, format="JPEG", quality=90, optimize=True)
            elif save_fmt == "WEBP":
                clean_img.save(out, format="WEBP", quality=90)
            else:
                clean_img.save(out, format="PNG")
            clean_bytes = out.getvalue()
    except ValidationFailedError:
        raise
    except Exception as exc:
        raise ValidationFailedError("Failed to decode and sanitize image.") from exc

    return SanitizedImage(
        clean_bytes=clean_bytes,
        mime_type=mime_type,
        extension=ext,
        width=width,
        height=height,
    )
