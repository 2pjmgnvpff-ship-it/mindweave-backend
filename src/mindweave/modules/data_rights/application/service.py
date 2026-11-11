"""Application service for PIPL Art. 45/47 data export (GB 45438-2025) & crypto-shredding deletion."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import ForbiddenError, NotFoundError
from mindweave.core.ids import new_id
from mindweave.core.orm import BackgroundJobRow, ExportJobRow
from mindweave.core.security import hash_opaque_token
from mindweave.modules.audit.application.service import AuditService, anonymize_actor_id
from mindweave.modules.data_rights.domain.models import (
    ExportMediaItem,
    allocate_media_archive_path,
    package_export_zip,
    verify_one_time_download_token,
)
from mindweave.modules.data_rights.infra.repository import DataRightsRepository
from mindweave.modules.telemetry.application.service import TelemetryService
from mindweave.shared.enums import ExportStatus
from mindweave.shared.scopes import UserScope
from mindweave.workers.ports import TASK_BUILD_EXPORT_ARCHIVE, JobQueuePort


@dataclass(frozen=True, slots=True)
class BuiltExportResult:
    export_id: UUID
    status: str
    byte_size: int | None
    sha256_hex: str | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    download_token: str | None


@dataclass(frozen=True, slots=True)
class DeleteAccountResult:
    deleted: bool
    crypto_shredded: bool
    deleted_blobs_count: int
    cancelled_jobs_count: int
    deleted_at: datetime
    audit_receipt_id: UUID


class DataRightsService:
    """Orchestrates encrypted export archive generation, one-time downloads, and crypto-shredding."""

    def __init__(
        self,
        container: AppContainer,
        *,
        repo: DataRightsRepository | None = None,
    ) -> None:
        self._container = container
        self._repo = repo or DataRightsRepository()

    def _discover_unreferenced_local_blobs(
        self,
        *,
        user_id: UUID,
        known_keys: list[str],
    ) -> list[str]:
        merged = list(known_keys)
        seen = set(known_keys)
        user_dir: Path = (self._container.settings.blob_storage_dir / str(user_id)).resolve()
        root_dir: Path = self._container.settings.blob_storage_dir.resolve()
        if not str(user_dir).startswith(str(root_dir)) or not user_dir.is_dir():
            return merged
        for file_path in sorted(user_dir.rglob("*")):
            if not file_path.is_file() or file_path.name.endswith(".ct"):
                continue
            rel_key = file_path.relative_to(root_dir).as_posix()
            if "/exports/" in f"/{rel_key}" or rel_key in seen:
                continue
            seen.add(rel_key)
            merged.append(rel_key)
        return merged

    async def create_export(
        self,
        *,
        scope: UserScope,
        async_mode: bool = False,
        client_ip: str | None = None,
    ) -> BuiltExportResult:
        now = self._container.clock.now()
        export_id = new_id()

        if async_mode:
            async with self._container.db.user_session(scope) as user_sess:
                row = await self._repo.create_export_job(
                    user_sess,
                    export_id=export_id,
                    scope=scope,
                    status=ExportStatus.PENDING.value,
                    created_at=now,
                )
                telemetry = TelemetryService(clock=self._container.clock)
                await telemetry.record_event(
                    user_sess,
                    scope=scope,
                    event_name="data_export",
                    metadata={"status": ExportStatus.PENDING.value},
                    occurred_at=now,
                )
                created_at = row.created_at

            async with self._container.db.system_session() as sys_sess:
                queue: JobQueuePort | None = self._container.extras.get("job_queue")
                payload = {"export_id": str(export_id), "user_id": str(scope.user_id)}
                if queue is not None:
                    await queue.enqueue(
                        sys_sess,
                        task_name=TASK_BUILD_EXPORT_ARCHIVE,
                        payload=payload,
                        user_id=scope.user_id,
                        idempotency_key=f"export:{export_id}",
                    )
                else:
                    sys_sess.add(
                        BackgroundJobRow(
                            id=new_id(),
                            user_id=scope.user_id,
                            task_name=TASK_BUILD_EXPORT_ARCHIVE,
                            payload_json=payload,
                            idempotency_key=f"export:{export_id}",
                            status="queued",
                            run_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                audit = AuditService(
                    clock=self._container.clock,
                    secret=self._container.settings.jwt_secret,
                )
                await audit.record(
                    sys_sess,
                    user_id=scope.user_id,
                    action="data_rights.export_queued",
                    target_type="export_job",
                    target_id=str(export_id),
                    metadata={"status": ExportStatus.PENDING.value},
                    client_ip=client_ip,
                )

            return BuiltExportResult(
                export_id=export_id,
                status=ExportStatus.PENDING.value,
                byte_size=None,
                sha256_hex=None,
                created_at=created_at,
                completed_at=None,
                expires_at=None,
                download_token=None,
            )

        return await self.build_export_archive(
            scope=scope,
            export_id=export_id,
            client_ip=client_ip,
        )

    async def build_export_archive(
        self,
        *,
        scope: UserScope,
        export_id: UUID | None = None,
        client_ip: str | None = None,
    ) -> BuiltExportResult:
        now = self._container.clock.now()
        target_export_id = export_id or new_id()
        expires_at = now + timedelta(hours=self._container.settings.export_download_ttl_hours)
        download_token = secrets.token_urlsafe(32)
        token_hash = hash_opaque_token(download_token)
        archive_object_key = f"{scope.user_id}/exports/{target_export_id}.zip"

        async with (
            self._container.db.user_session(scope) as user_sess,
            self._container.db.system_session() as sys_sess,
        ):
            dek = await self._container.load_user_dek(user_sess, scope)
            job_row = await self._repo.get_export_job(
                user_sess,
                scope=scope,
                export_id=target_export_id,
                for_update=True,
            )
            if job_row is None:
                job_row = await self._repo.create_export_job(
                    user_sess,
                    export_id=target_export_id,
                    scope=scope,
                    status=ExportStatus.PROCESSING.value,
                    created_at=now,
                )
            else:
                job_row.status = ExportStatus.PROCESSING.value

            # Record telemetry before collecting exportable sections so the export itself is complete
            telemetry = TelemetryService(clock=self._container.clock)
            await telemetry.record_event(
                user_sess,
                scope=scope,
                event_name="data_export",
                metadata={"status": ExportStatus.COMPLETED.value},
                occurred_at=now,
            )

            sections, referenced_media_keys = await self._repo.collect_exportable_user_data(
                user_sess,
                sys_sess,
                scope=scope,
                cipher=self._container.cipher,
                dek=dek,
            )
            all_media_keys = self._discover_unreferenced_local_blobs(
                user_id=scope.user_id,
                known_keys=referenced_media_keys,
            )

            used_arc_paths: set[str] = set()
            media_items: list[ExportMediaItem] = []
            for obj_key in all_media_keys:
                try:
                    blob_bytes, content_type = await self._container.storage.get_decrypted(
                        user_id=scope.user_id,
                        dek=dek,
                        object_key=obj_key,
                    )
                except NotFoundError:
                    continue
                arc_path = allocate_media_archive_path(obj_key, used_arc_paths)
                media_items.append(
                    ExportMediaItem(
                        object_key=obj_key,
                        archive_path=arc_path,
                        content_type=content_type,
                        data=blob_bytes,
                    )
                )

            bundle = package_export_zip(
                export_id=str(target_export_id),
                user_id=str(scope.user_id),
                exported_at_iso=now.isoformat(),
                sections=sections,
                media_items=media_items,
            )

            await self._container.storage.put_encrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=archive_object_key,
                plaintext=bundle.zip_bytes,
                content_type="application/zip",
            )

            job_row.status = ExportStatus.COMPLETED.value
            job_row.archive_object_key = archive_object_key
            job_row.download_token_hash = token_hash
            job_row.byte_size = len(bundle.zip_bytes)
            job_row.sha256_hex = bundle.zip_sha256_hex
            job_row.expires_at = expires_at
            job_row.completed_at = now
            await user_sess.flush()

            audit = AuditService(
                clock=self._container.clock,
                secret=self._container.settings.jwt_secret,
            )
            await audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="data_rights.export_created",
                target_type="export_job",
                target_id=str(target_export_id),
                metadata={
                    "byteSize": len(bundle.zip_bytes),
                    "sha256Hex": bundle.zip_sha256_hex,
                    "mediaCount": len(media_items),
                    "aigcStandard": "GB 45438-2025",
                },
                client_ip=client_ip,
            )

            return BuiltExportResult(
                export_id=job_row.id,
                status=job_row.status,
                byte_size=job_row.byte_size,
                sha256_hex=job_row.sha256_hex,
                created_at=job_row.created_at,
                completed_at=job_row.completed_at,
                expires_at=job_row.expires_at,
                download_token=download_token,
            )

    async def get_export_job(
        self,
        *,
        scope: UserScope,
        export_id: UUID,
        issue_download_token: bool = False,
    ) -> tuple[ExportJobRow, str | None]:
        now = self._container.clock.now()
        async with self._container.db.user_session(scope) as session:
            row = await self._repo.get_export_job(
                session,
                scope=scope,
                export_id=export_id,
                for_update=issue_download_token,
            )
            if row is None:
                raise NotFoundError("Export job not found")
            fresh_token: str | None = None
            if (
                issue_download_token
                and row.status == ExportStatus.COMPLETED.value
                and row.downloaded_at is None
                and (row.expires_at is None or row.expires_at > now)
            ):
                fresh_token = secrets.token_urlsafe(32)
                row.download_token_hash = hash_opaque_token(fresh_token)
                await session.flush()
            return row, fresh_token

    async def list_export_jobs(
        self,
        *,
        scope: UserScope,
    ) -> list[ExportJobRow]:
        async with self._container.db.user_session(scope) as session:
            return await self._repo.list_export_jobs(session, scope=scope)

    async def download_export(
        self,
        *,
        scope: UserScope,
        export_id: UUID,
        token: str,
        client_ip: str | None = None,
    ) -> tuple[bytes, str]:
        now = self._container.clock.now()
        async with self._container.db.user_session(scope) as user_sess:
            row = await self._repo.get_export_job(
                user_sess,
                scope=scope,
                export_id=export_id,
                for_update=True,
            )
            if row is None:
                raise NotFoundError("Export job not found")

            rejection = verify_one_time_download_token(
                provided_token=token,
                stored_token_hash=row.download_token_hash,
                status=row.status,
                expires_at=row.expires_at,
                downloaded_at=row.downloaded_at,
                now=now,
            )
            if rejection is not None:
                raise ForbiddenError(
                    f"Export download token rejected ({rejection}).",
                    error_code=f"export_{rejection}",
                )

            if not row.archive_object_key:
                raise NotFoundError("Export archive object not found")

            consumed = await self._repo.consume_download_token_once(
                user_sess,
                scope=scope,
                export_id=export_id,
                downloaded_at=now,
            )
            if not consumed:
                raise ForbiddenError(
                    "Export download token has already been used.",
                    error_code="export_already_downloaded",
                )

            dek = await self._container.load_user_dek(user_sess, scope)
            zip_bytes, _ = await self._container.storage.get_decrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=row.archive_object_key,
            )

        async with self._container.db.system_session() as sys_sess:
            audit = AuditService(
                clock=self._container.clock,
                secret=self._container.settings.jwt_secret,
            )
            await audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="data_rights.export_downloaded",
                target_type="export_job",
                target_id=str(export_id),
                metadata={"byteSize": len(zip_bytes)},
                client_ip=client_ip,
            )

        filename = f"mindweave-export-{export_id}.zip"
        return zip_bytes, filename

    async def crypto_shred_user_key(self, *, scope: UserScope) -> datetime:
        """Destroy user's wrapped DEK in DB and zeroize in-memory DEK cache immediately."""
        now = self._container.clock.now()
        self._container.cipher.evict_user_dek(scope.user_id)
        async with self._container.db.user_session(scope) as user_sess:
            await self._repo.shred_user_key_in_session(
                user_sess,
                scope=scope,
                destroyed_at=now,
            )
        self._container.cipher.evict_user_dek(scope.user_id)
        return now

    async def delete_account(
        self,
        *,
        scope: UserScope,
        client_ip: str | None = None,
    ) -> DeleteAccountResult:
        """Irreversibly crypto-shred DEK, purge media blobs, cascade-delete all user rows, and write audit tombstone."""
        now = self._container.clock.now()

        # Step 1: Evict in-memory DEK & overwrite UserKeyRow(wrapped_dek=None, destroyed_at=now) inside user_session first!
        self._container.cipher.evict_user_dek(scope.user_id)
        async with self._container.db.user_session(scope) as user_sess:
            await self._repo.shred_user_key_in_session(
                user_sess,
                scope=scope,
                destroyed_at=now,
            )
        self._container.cipher.evict_user_dek(scope.user_id)

        # Step 2: Delete all encrypted media blobs under user prefix
        deleted_blobs = await self._container.storage.delete_user_prefix(user_id=scope.user_id)

        # Step 3: Cancel queued BackgroundJobRows, physically delete UserRow (cascading across all 21 user tables),
        # and write zero-PII anonymized compliance tombstone in AuditLogRow.
        async with self._container.db.system_session() as sys_sess:
            cancelled_jobs = await self._repo.cancel_queued_jobs_and_delete_user(
                sys_sess,
                user_id=scope.user_id,
                now=now,
            )
            anonymized_target = anonymize_actor_id(
                scope.user_id,
                secret=self._container.settings.jwt_secret,
            )[:32]
            audit = AuditService(
                clock=self._container.clock,
                secret=self._container.settings.jwt_secret,
            )
            tombstone = await audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="data_rights.account_deleted",
                target_type="user_account",
                target_id=anonymized_target,
                metadata={
                    "cryptoShredded": True,
                    "deletedBlobsCount": deleted_blobs,
                    "cancelledJobsCount": cancelled_jobs,
                    "status": "shredded",
                },
                client_ip=client_ip,
            )
            receipt_id = tombstone.id

        # Step 4: Final memory cache eviction guard
        self._container.cipher.evict_user_dek(scope.user_id)

        return DeleteAccountResult(
            deleted=True,
            crypto_shredded=True,
            deleted_blobs_count=deleted_blobs,
            cancelled_jobs_count=cancelled_jobs,
            deleted_at=now,
            audit_receipt_id=receipt_id,
        )


def _coerce_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


async def build_export_archive(
    container: AppContainer,
    user_id: UUID | str | None = None,
    export_id: UUID | str | None = None,
    *,
    job: object | None = None,
    client_ip: str | None = None,
) -> BuiltExportResult:
    """Worker/direct entrypoint for `TASK_BUILD_EXPORT_ARCHIVE`."""
    resolved_user_id: UUID | str | None = user_id
    resolved_export_id: UUID | str | None = export_id
    if job is not None:
        payload = getattr(job, "payload", None)
        if isinstance(payload, dict):
            if resolved_user_id is None and payload.get("user_id"):
                resolved_user_id = str(payload["user_id"])
            if resolved_export_id is None and payload.get("export_id"):
                resolved_export_id = str(payload["export_id"])
        if resolved_user_id is None:
            job_uid = getattr(job, "user_id", None)
            if isinstance(job_uid, UUID | str):
                resolved_user_id = job_uid
    if resolved_user_id is None:
        raise ValueError("user_id is required to build export archive")

    uid = _coerce_uuid(resolved_user_id)
    eid = _coerce_uuid(resolved_export_id) if resolved_export_id is not None else None
    service = DataRightsService(container)
    return await service.build_export_archive(
        scope=UserScope(user_id=uid),
        export_id=eid,
        client_ip=client_ip,
    )


def make_build_export_handler(container: AppContainer) -> object:
    """Return an async `ClaimedJob` handler for `WorkerRunner.register_handler`."""

    async def _handle(job: object) -> BuiltExportResult:
        return await build_export_archive(container, job=job)

    return _handle
