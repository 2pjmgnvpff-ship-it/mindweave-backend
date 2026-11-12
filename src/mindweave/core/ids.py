"""Monotonic RFC 9562 UUIDv7 generator and validator (Invariant I11)."""

from __future__ import annotations

import os
import threading
import time
from uuid import UUID

_lock = threading.Lock()
_last_ms = 0
_last_rand_a = 0


def new_id() -> UUID:
    """Generate a time-ordered, process-monotonic RFC 9562 UUIDv7."""
    global _last_ms, _last_rand_a
    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _last_rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
        else:
            _last_rand_a += 1
            if _last_rand_a > 0x0FFF:
                _last_ms += 1
                _last_rand_a = 0
        ts_ms = _last_ms & ((1 << 48) - 1)
        rand_a = _last_rand_a & 0x0FFF

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (
        (ts_ms << 80)
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122 / 9562 variant
        | rand_b
    )
    return UUID(int=value)


def is_uuid7(u: UUID) -> bool:
    return u.version == 7
