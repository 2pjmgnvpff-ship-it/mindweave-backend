"""CamelCase API DTOs for MindWeave data sovereignty endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from mindweave.core.dto import CamelModel


class CreateExportRequest(CamelModel):
    async_mode: bool = False


class ExportJobResponse(CamelModel):
    export_id: UUID
    status: str
    byte_size: int | None = None
    sha256_hex: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    downloaded_at: datetime | None = None
    download_token: str | None = None
    download_path: str | None = None


class ExportJobListResponse(CamelModel):
    items: list[ExportJobResponse]


class DeleteAccountResponse(CamelModel):
    deleted: bool = True
    crypto_shredded: bool = True
    deleted_blobs_count: int
    cancelled_jobs_count: int
    deleted_at: datetime
    audit_receipt_id: UUID
