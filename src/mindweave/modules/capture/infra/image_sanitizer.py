"""Re-export Pillow image validation and EXIF stripper for capture pipeline."""

from mindweave.modules.memory.infra.image_sanitizer import (
    SanitizedImage,
    validate_and_strip_image_exif,
)

__all__ = ["SanitizedImage", "validate_and_strip_image_exif"]
