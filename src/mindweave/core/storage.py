"""Encrypted Object Storage Port & Adapters (Invariant I12).

All user media (voice recordings, kept screenshots, custom wallpapers, export ZIPs)
are encrypted in-memory with the user's DEK + AAD (`{user_id}:blob:{object_key}`)
BEFORE being written to storage, ensuring zero plaintext media at rest and instant
cryptographic destruction when the user's DEK is shredded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol
from uuid import UUID

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.errors import NotFoundError


class ObjectStoragePort(Protocol):
    async def put_encrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
        plaintext: bytes,
        content_type: str,
    ) -> None: ...

    async def get_decrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
    ) -> tuple[bytes, str]: ...

    async def delete_object(self, *, object_key: str) -> None: ...

    async def delete_user_prefix(self, *, user_id: UUID) -> int: ...


def _safe_rel_key(object_key: str) -> Path:
    cleaned = object_key.strip().lstrip("/")
    rel = Path(cleaned)
    if not cleaned or ".." in rel.parts:
        raise ValueError(f"Unsafe object_key: {object_key!r}")
    return rel


class LocalEncryptedBlobStore:
    """Filesystem-backed encrypted blob store (dev/test/single-node)."""

    def __init__(self, root_dir: Path, cipher: EnvelopeCipher) -> None:
        self._root = root_dir.resolve()
        self._cipher = cipher
        self._root.mkdir(parents=True, exist_ok=True)

    def _paths(self, object_key: str) -> tuple[Path, Path]:
        rel = _safe_rel_key(object_key)
        blob_path = (self._root / rel).resolve()
        if not blob_path.is_relative_to(self._root):
            raise ValueError("Path traversal blocked")
        meta_path = blob_path.with_name(blob_path.name + ".ct")
        return blob_path, meta_path

    async def put_encrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
        plaintext: bytes,
        content_type: str,
    ) -> None:
        aad = self._cipher.blob_aad(user_id=user_id, object_key=object_key)
        ciphertext = self._cipher.encrypt_bytes(dek, plaintext, aad=aad)
        blob_path, meta_path = self._paths(object_key)

        def _write() -> None:
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(ciphertext)
            meta_path.write_text(content_type, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def get_decrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
    ) -> tuple[bytes, str]:
        blob_path, meta_path = self._paths(object_key)

        def _read() -> tuple[bytes, str]:
            if not blob_path.exists():
                raise NotFoundError("Media object not found")
            ct = blob_path.read_bytes()
            ctype = (
                meta_path.read_text(encoding="utf-8").strip()
                if meta_path.exists()
                else "application/octet-stream"
            )
            return ct, ctype

        ciphertext, content_type = await asyncio.to_thread(_read)
        aad = self._cipher.blob_aad(user_id=user_id, object_key=object_key)
        plaintext = self._cipher.decrypt_bytes(dek, ciphertext, aad=aad)
        return plaintext, content_type

    async def delete_object(self, *, object_key: str) -> None:
        blob_path, meta_path = self._paths(object_key)

        def _rm() -> None:
            blob_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        await asyncio.to_thread(_rm)

    async def delete_user_prefix(self, *, user_id: UUID) -> int:
        user_dir = (self._root / str(user_id)).resolve()
        if not user_dir.is_relative_to(self._root) or not user_dir.exists():
            return 0

        def _purge() -> int:
            count = 0
            for p in sorted(user_dir.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    if not p.name.endswith(".ct"):
                        count += 1
                elif p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
            try:
                user_dir.rmdir()
            except OSError:
                pass
            return count

        return await asyncio.to_thread(_purge)


async def filter_unreferenced_blob_keys(
    session: object,
    candidate_keys: set[str],
) -> list[str]:
    """Return only blob keys no longer referenced by any Record, Capture, Person, Preference, or Export."""
    cleaned = {k for k in candidate_keys if k}
    if not cleaned:
        return []
    from sqlalchemy import or_, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from mindweave.core.orm import (
        CaptureRow,
        ExportJobRow,
        PersonRow,
        RecordRow,
        UserPreferenceRow,
    )
    from mindweave.shared.enums import CaptureStatus

    assert isinstance(session, AsyncSession)
    keys_list = list(cleaned)
    live_record_keys = set(
        (
            await session.scalars(
                select(RecordRow.media_object_key).where(
                    RecordRow.media_object_key.in_(keys_list),
                )
            )
        ).all()
    )
    live_capture_audio = set(
        (
            await session.scalars(
                select(CaptureRow.audio_object_key).where(
                    CaptureRow.status != CaptureStatus.REVERTED.value,
                    CaptureRow.audio_object_key.in_(keys_list),
                )
            )
        ).all()
    )
    live_capture_img = set(
        (
            await session.scalars(
                select(CaptureRow.image_object_key).where(
                    CaptureRow.status != CaptureStatus.REVERTED.value,
                    CaptureRow.image_object_key.in_(keys_list),
                )
            )
        ).all()
    )
    person_rows = (
        await session.execute(
            select(
                PersonRow.avatar_object_key,
                PersonRow.main_photo_object_key,
                PersonRow.sub_photo_object_key,
            ).where(
                or_(
                    PersonRow.avatar_object_key.in_(keys_list),
                    PersonRow.main_photo_object_key.in_(keys_list),
                    PersonRow.sub_photo_object_key.in_(keys_list),
                )
            )
        )
    ).all()
    live_person_keys = {k for row in person_rows for k in row if k}
    live_wallpaper_keys = set(
        (
            await session.scalars(
                select(UserPreferenceRow.wallpaper_object_key).where(
                    UserPreferenceRow.wallpaper_object_key.in_(keys_list),
                )
            )
        ).all()
    )
    live_export_keys = set(
        (
            await session.scalars(
                select(ExportJobRow.archive_object_key).where(
                    ExportJobRow.archive_object_key.in_(keys_list),
                )
            )
        ).all()
    )
    still_live = {
        k
        for k in (
            live_record_keys
            | live_capture_audio
            | live_capture_img
            | live_person_keys
            | live_wallpaper_keys
            | live_export_keys
        )
        if k
    }
    return sorted(k for k in cleaned if k not in still_live)

