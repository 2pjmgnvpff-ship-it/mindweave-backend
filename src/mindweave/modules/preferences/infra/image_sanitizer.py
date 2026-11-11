"""Magic-byte image sniffer and EXIF/GPS/metadata stripper using Pillow."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from mindweave.core.errors import PayloadTooLargeError, ValidationFailedError

ALLOWED_WALLPAPER_FORMATS: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
MAX_WALLPAPER_PIXELS = 36_000_000  # 6000x6000 decompression-bomb guard


@dataclass(frozen=True, slots=True)
class SanitizedImage:
    data: bytes
    content_type: str
    format_name: str
    width: int
    height: int


def sanitize_wallpaper_image(raw_bytes: bytes, *, max_bytes: int) -> SanitizedImage:
    """Validate JPEG/PNG/WEBP magic bytes and strip all EXIF/XMP/ICC metadata via pixel re-encoding."""
    if not raw_bytes:
        raise ValidationFailedError("Uploaded wallpaper image cannot be empty")
    if len(raw_bytes) > max_bytes:
        raise PayloadTooLargeError(
            f"Wallpaper image exceeds maximum allowed size ({max_bytes} bytes)"
        )

    try:
        with Image.open(io.BytesIO(raw_bytes)) as src:
            detected_format = (src.format or "").upper()
            if detected_format not in ALLOWED_WALLPAPER_FORMATS:
                raise ValidationFailedError(
                    "Unsupported image format; only JPEG, PNG, and WEBP are allowed"
                )
            width, height = src.size
            if width <= 0 or height <= 0 or (width * height) > MAX_WALLPAPER_PIXELS:
                raise ValidationFailedError("Image dimensions are invalid or exceed safety limits")

            src.load()
            oriented = ImageOps.exif_transpose(src)

            if detected_format == "JPEG":
                target_mode = "RGB"
            else:
                has_alpha = "A" in oriented.getbands() or (
                    oriented.mode == "P" and "transparency" in oriented.info
                )
                target_mode = "RGBA" if has_alpha else "RGB"

            converted = oriented if oriented.mode == target_mode else oriented.convert(target_mode)
            # Construct a brand-new image strictly from raw pixel bytes (zero EXIF/XMP/chunks!)
            clean_image = Image.frombytes(target_mode, converted.size, converted.tobytes())
            clean_width, clean_height = clean_image.size

            out = io.BytesIO()
            if detected_format == "JPEG":
                clean_image.save(out, format="JPEG", quality=88, optimize=True)
            elif detected_format == "PNG":
                clean_image.save(out, format="PNG", optimize=True)
            else:
                clean_image.save(out, format="WEBP", quality=88)

            clean_bytes = out.getvalue()
    except (ValidationFailedError, PayloadTooLargeError):
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ValidationFailedError(
            "Invalid or corrupted image file; only valid JPEG, PNG, or WEBP files are accepted"
        ) from exc

    return SanitizedImage(
        data=clean_bytes,
        content_type=ALLOWED_WALLPAPER_FORMATS[detected_format],
        format_name=detected_format,
        width=clean_width,
        height=clean_height,
    )
