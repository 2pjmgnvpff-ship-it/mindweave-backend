"""Infrastructure adapters for the People module."""

from mindweave.modules.people.infra.media import ProcessedPhoto, verify_and_strip_exif
from mindweave.modules.people.infra.repository import ALIAS_BLIND_INDEX_DOMAIN, PeopleRepository

__all__ = [
    "ALIAS_BLIND_INDEX_DOMAIN",
    "PeopleRepository",
    "ProcessedPhoto",
    "verify_and_strip_exif",
]
