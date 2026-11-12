"""Per-user Envelope Encryption, Blind Indexing & Crypto-Shredding (Invariant I12).

Format of encrypted bytes (`*_enc` columns and encrypted blobs):
    version_byte (0x01) || nonce (12 bytes) || ciphertext || gcm_tag (16 bytes)

Every encryption operation binds AAD (`associated_data`) to prevent ciphertext
transplantation across users, tables, columns, or row IDs:
    field AAD = f"{user_id}:{table}:{column}:{row_id}".encode("utf-8")
    blob  AAD = f"{user_id}:blob:{object_key}".encode("utf-8")
    dek   AAD = f"user_keys:wrapped_dek:{user_id}".encode("utf-8")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION_V1 = 0x01
NONCE_LEN = 12
MIN_CIPHERTEXT_LEN = 1 + NONCE_LEN + 16  # version + nonce + 16-byte GCM tag


class CryptoError(Exception):
    """Raised when decryption fails, AAD mismatches, or user key was shredded."""


class KeyShreddedError(CryptoError):
    """Raised when attempting to access data whose per-user DEK has been destroyed."""


class KekProvider(Protocol):
    @property
    def kek_id(self) -> str: ...

    def wrap_dek(self, *, user_id: UUID, dek: bytes) -> bytes: ...

    def unwrap_dek(self, *, user_id: UUID, wrapped_dek: bytes, kek_id: str) -> bytes: ...


class EnvKekProvider:
    """Local/env KEK provider using AES-256-GCM key wrapping."""

    def __init__(self, *, kek_hex: str, kek_id: str = "kek-local-v1") -> None:
        raw = bytes.fromhex(kek_hex)
        if len(raw) != 32:
            raise ValueError("KEK must be 32 bytes (64 hex chars)")
        self._kek = raw
        self._aes = AESGCM(raw)
        self._kek_id = kek_id

    @property
    def kek_id(self) -> str:
        return self._kek_id

    def wrap_dek(self, *, user_id: UUID, dek: bytes) -> bytes:
        if len(dek) != 32:
            raise ValueError("DEK must be 32 bytes")
        nonce = os.urandom(NONCE_LEN)
        aad = f"user_keys:wrapped_dek:{user_id}:{self._kek_id}".encode()
        ct = self._aes.encrypt(nonce, dek, aad)
        return bytes([ENVELOPE_VERSION_V1]) + nonce + ct

    def unwrap_dek(self, *, user_id: UUID, wrapped_dek: bytes, kek_id: str) -> bytes:
        if kek_id != self._kek_id:
            raise CryptoError(f"Unknown KEK id {kek_id!r} (expected {self._kek_id!r})")
        if len(wrapped_dek) < MIN_CIPHERTEXT_LEN or wrapped_dek[0] != ENVELOPE_VERSION_V1:
            raise CryptoError("Malformed wrapped DEK header")
        nonce = wrapped_dek[1 : 1 + NONCE_LEN]
        ct = wrapped_dek[1 + NONCE_LEN :]
        aad = f"user_keys:wrapped_dek:{user_id}:{kek_id}".encode()
        try:
            dek = self._aes.decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            raise CryptoError("Wrapped DEK authentication failed") from exc
        if len(dek) != 32:
            raise CryptoError("Unwrapped DEK has invalid length")
        return dek


class AliyunKmsStubProvider:
    """M6 adapter skeleton for Alibaba Cloud KMS Envelope Key Wrapping."""

    def __init__(self, *, key_id: str) -> None:
        self._key_id = key_id

    @property
    def kek_id(self) -> str:
        return self._key_id

    def wrap_dek(self, *, user_id: UUID, dek: bytes) -> bytes:
        raise NotImplementedError("Enable aliyun-python-sdk-kms in M6 mainland deployment")

    def unwrap_dek(self, *, user_id: UUID, wrapped_dek: bytes, kek_id: str) -> bytes:
        raise NotImplementedError("Enable aliyun-python-sdk-kms in M6 mainland deployment")


@dataclass(slots=True)
class _CachedDek:
    dek: bytearray
    blind_subkey: bytes
    expires_at: float

    def zeroize(self) -> None:
        for i in range(len(self.dek)):
            self.dek[i] = 0


class EnvelopeCipher:
    """Thread-safe per-user AES-256-GCM cipher with bounded TTL DEK cache & instant shred."""

    def __init__(
        self,
        kek_provider: KekProvider,
        *,
        cache_ttl_seconds: int = 300,
        cache_max_entries: int = 2048,
    ) -> None:
        self._kek = kek_provider
        self._ttl = cache_ttl_seconds
        self._max_entries = cache_max_entries
        self._lock = threading.Lock()
        self._cache: OrderedDict[UUID, _CachedDek] = OrderedDict()

    @property
    def kek_id(self) -> str:
        return self._kek.kek_id

    def generate_wrapped_dek(self, user_id: UUID) -> tuple[bytes, str]:
        dek = os.urandom(32)
        wrapped = self._kek.wrap_dek(user_id=user_id, dek=dek)
        self._put_cache(user_id, dek)
        return wrapped, self._kek.kek_id

    def load_dek(
        self,
        *,
        user_id: UUID,
        wrapped_dek: bytes | None,
        kek_id: str,
        destroyed_at: object | None = None,
    ) -> bytes:
        if destroyed_at is not None or wrapped_dek is None:
            self.evict_user_dek(user_id)
            raise KeyShreddedError(f"DEK for user {user_id} has been cryptographically shredded")

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached is not None:
                if cached.expires_at > now:
                    self._cache.move_to_end(user_id)
                    return bytes(cached.dek)
                cached.zeroize()
                del self._cache[user_id]

        dek = self._kek.unwrap_dek(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id)
        self._put_cache(user_id, dek)
        return dek

    def evict_user_dek(self, user_id: UUID) -> None:
        """Zeroize and immediately purge a user's DEK from memory cache (Crypto-Shredding)."""
        with self._lock:
            cached = self._cache.pop(user_id, None)
            if cached is not None:
                cached.zeroize()

    def clear_cache(self) -> None:
        with self._lock:
            for cached in self._cache.values():
                cached.zeroize()
            self._cache.clear()

    def _put_cache(self, user_id: UUID, dek: bytes) -> None:
        subkey = _derive_blind_subkey(dek, user_id)
        with self._lock:
            old = self._cache.pop(user_id, None)
            if old is not None:
                old.zeroize()
            while len(self._cache) >= self._max_entries:
                _, evicted = self._cache.popitem(last=False)
                evicted.zeroize()
            self._cache[user_id] = _CachedDek(
                dek=bytearray(dek),
                blind_subkey=subkey,
                expires_at=time.monotonic() + self._ttl,
            )

    @staticmethod
    def field_aad(*, user_id: UUID, table: str, column: str, row_id: UUID | str) -> bytes:
        return f"{user_id}:{table}:{column}:{row_id}".encode()

    @staticmethod
    def blob_aad(*, user_id: UUID, object_key: str) -> bytes:
        return f"{user_id}:blob:{object_key}".encode()

    @staticmethod
    def encrypt_bytes(dek: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return bytes([ENVELOPE_VERSION_V1]) + nonce + ct

    @staticmethod
    def decrypt_bytes(dek: bytes, blob: bytes, *, aad: bytes) -> bytes:
        if len(blob) < MIN_CIPHERTEXT_LEN or blob[0] != ENVELOPE_VERSION_V1:
            raise CryptoError("Invalid ciphertext envelope version or length")
        nonce = blob[1 : 1 + NONCE_LEN]
        ct = blob[1 + NONCE_LEN :]
        try:
            return AESGCM(dek).decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            raise CryptoError("Ciphertext authentication or AAD binding failed") from exc

    def encrypt_str(
        self,
        dek: bytes,
        plaintext: str,
        *,
        user_id: UUID,
        table: str,
        column: str,
        row_id: UUID | str,
    ) -> bytes:
        aad = self.field_aad(user_id=user_id, table=table, column=column, row_id=row_id)
        return self.encrypt_bytes(dek, plaintext.encode("utf-8"), aad=aad)

    def decrypt_str(
        self,
        dek: bytes,
        ciphertext: bytes,
        *,
        user_id: UUID,
        table: str,
        column: str,
        row_id: UUID | str,
    ) -> str:
        aad = self.field_aad(user_id=user_id, table=table, column=column, row_id=row_id)
        return self.decrypt_bytes(dek, ciphertext, aad=aad).decode("utf-8")

    def encrypt_json(
        self,
        dek: bytes,
        payload: Any,
        *,
        user_id: UUID,
        table: str,
        column: str,
        row_id: UUID | str,
    ) -> bytes:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self.encrypt_str(
            dek,
            serialized,
            user_id=user_id,
            table=table,
            column=column,
            row_id=row_id,
        )

    def decrypt_json(
        self,
        dek: bytes,
        ciphertext: bytes,
        *,
        user_id: UUID,
        table: str,
        column: str,
        row_id: UUID | str,
    ) -> Any:
        raw = self.decrypt_str(
            dek,
            ciphertext,
            user_id=user_id,
            table=table,
            column=column,
            row_id=row_id,
        )
        return json.loads(raw)

    @staticmethod
    def compute_blind_index(
        dek: bytes,
        *,
        user_id: UUID,
        domain: str,
        normalized_value: str,
    ) -> bytes:
        """Compute tenant-isolated HMAC-SHA256 blind index derived from user's DEK."""
        subkey = _derive_blind_subkey(dek, user_id)
        msg = f"{domain}:{normalized_value}".encode()
        return hmac.new(subkey, msg, hashlib.sha256).digest()


def normalize_alias_for_index(text: str) -> str:
    """Canonical normalization for alias/name blind indexing."""
    return "".join(text.strip().lower().split())


def _derive_blind_subkey(dek: bytes, user_id: UUID) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=user_id.bytes,
        info=b"mindweave-blind-index-v1",
    )
    return hkdf.derive(dek)
