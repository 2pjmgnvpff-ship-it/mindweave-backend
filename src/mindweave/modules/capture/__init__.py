"""Multimodal Capture Pipeline module (PEP 562 lazy export avoids circular imports)."""

from __future__ import annotations

from typing import Any

__all__ = ["handle_process_capture_job"]


def __getattr__(name: str) -> Any:
    if name == "handle_process_capture_job":
        from mindweave.modules.capture.application.service import handle_process_capture_job

        return handle_process_capture_job
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
