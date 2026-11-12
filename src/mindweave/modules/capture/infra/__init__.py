"""Infrastructure exports for the capture module."""

from mindweave.modules.capture.infra.image_sanitizer import (
    SanitizedImage,
    validate_and_strip_image_exif,
)
from mindweave.modules.capture.infra.repository import (
    get_or_create_unassigned_placeholder,
    load_known_person_profiles,
    revert_capture_graph,
)

__all__ = [
    "SanitizedImage",
    "get_or_create_unassigned_placeholder",
    "load_known_person_profiles",
    "revert_capture_graph",
    "validate_and_strip_image_exif",
]
