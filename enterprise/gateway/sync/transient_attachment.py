"""Conversation-scoped transient attachments.

Only metadata and hashed one-time tickets are stored in Enterprise SQLite.
Bytes remain in the configured S3-compatible object store and all browser
downloads go through the Gateway.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any, Awaitable, Callable, Protocol

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from enterprise.gateway.auth.middleware import (
    UserAuthError,
    require_capability,
    require_user_principal,
)
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config
from enterprise.gateway.query import v2_store
from enterprise.gateway.sync.source_adapter import (
    S3SourceAdapter,
    SourceFile,
    SourceTooLarge,
)


logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_DOWNLOADS = 1
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_CLEANUP_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_OVERHEAD_BYTES = 64 * 1024
ATTACHMENT_NOT_IMPLEMENTED_MESSAGE = (
    "Transient attachment is planned but not enabled"
)

ALLOWED_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".csv": frozenset({"text/csv"}),
    ".docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
    ".jpeg": frozenset({"image/jpeg"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".json": frozenset({"application/json"}),
    ".pdf": frozenset({"application/pdf"}),
    ".png": frozenset({"image/png"}),
    ".txt": frozenset({"text/plain"}),
    ".xlsx": frozenset(
        {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
}


class AttachmentStorage(Protocol):
    async def put_object(
        self, bucket: str, object_key: str, content: bytes, media_type: str
    ) -> None: ...

    async def fetch(
        self,
        bucket: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> SourceFile: ...

    async def delete_object(self, bucket: str, object_key: str) -> None: ...


class TransientAttachmentError(Exception):
    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_id: str
    tenant_id: str
    conversation_id: str
    business_user_id: str
    file_name: str
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    expires_at: str
    max_downloads: int
    download_count: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DownloadTicket:
    token: str
    attachment_id: str
    expires_at: str


@dataclass(frozen=True)
class DownloadedAttachment:
    record: AttachmentRecord
    content: bytes


class CreateAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fileName: str = Field(min_length=1, max_length=255)
    mediaType: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)


CREATE_TRANSIENT_ATTACHMENT = """
CREATE TABLE IF NOT EXISTS ext_transient_attachment (
    attachment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    object_bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    file_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_downloads INTEGER NOT NULL DEFAULT 1,
    download_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploading',
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    delete_attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ext_transient_attachment_expiry
    ON ext_transient_attachment(status, expires_at, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_ext_transient_attachment_owner
    ON ext_transient_attachment(tenant_id, conversation_id, business_user_id);

CREATE TABLE IF NOT EXISTS ext_transient_attachment_ticket (
    ticket_hash TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claim_expires_at TEXT,
    consumed_at TEXT,
    download_attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ext_transient_ticket_expiry
    ON ext_transient_attachment_ticket(expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS ext_transient_attachment_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    request_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ext_transient_audit_attachment
    ON ext_transient_attachment_audit(attachment_id, created_at);
"""


async def ensure_attachment_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(CREATE_TRANSIENT_ATTACHMENT)
    await db.commit()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def attachment_max_size_bytes() -> int:
    if os.getenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES") is not None:
        return _env_int(
            "ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES",
            DEFAULT_MAX_SIZE_BYTES,
        )
    if os.getenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_MB") is not None:
        return _env_int(
            "ENTERPRISE_ATTACHMENT_MAX_SIZE_MB",
            DEFAULT_MAX_SIZE_BYTES // (1024 * 1024),
        ) * 1024 * 1024
    return DEFAULT_MAX_SIZE_BYTES


def attachment_max_encoded_length() -> int:
    """Return the largest valid base64 representation for the decoded limit."""
    return 4 * ((attachment_max_size_bytes() + 2) // 3)


def attachment_max_request_body_bytes() -> int:
    """Bound JSON framing and metadata in addition to the base64 content."""
    return attachment_max_encoded_length() + DEFAULT_REQUEST_OVERHEAD_BYTES


def attachment_ttl_seconds() -> int:
    return _env_int("ENTERPRISE_ATTACHMENT_TTL_SECONDS", DEFAULT_TTL_SECONDS)


def attachment_max_downloads() -> int:
    return _env_int(
        "ENTERPRISE_ATTACHMENT_MAX_DOWNLOADS",
        DEFAULT_MAX_DOWNLOADS,
        maximum=100,
    )


def attachment_retry_attempts() -> int:
    return _env_int(
        "ENTERPRISE_ATTACHMENT_RETRY_ATTEMPTS",
        DEFAULT_RETRY_ATTEMPTS,
        maximum=10,
    )


def attachment_cleanup_interval_seconds() -> int:
    return _env_int(
        "ENTERPRISE_ATTACHMENT_CLEANUP_INTERVAL_SECONDS",
        DEFAULT_CLEANUP_INTERVAL_SECONDS,
    )


def _retry_delay_seconds(attempt: int) -> float:
    try:
        base = max(
            0.0,
            float(os.getenv("ENTERPRISE_ATTACHMENT_RETRY_DELAY_SECONDS", "1")),
        )
    except ValueError:
        base = 1.0
    return min(base * (2 ** max(attempt - 1, 0)), 300.0)


def _storage_bucket() -> str:
    return os.getenv("S3_TRANSIENT_BUCKET", "").strip() or os.getenv(
        "S3_BUCKET", ""
    ).strip()


def _storage_prefix() -> str:
    return (
        os.getenv("S3_TRANSIENT_PREFIX", "transient-attachments").strip("/")
        or "transient-attachments"
    )


def _storage() -> AttachmentStorage:
    return S3SourceAdapter(max_size_bytes=attachment_max_size_bytes())


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_file_name(file_name: str) -> tuple[str, str]:
    if (
        not isinstance(file_name, str)
        or not file_name
        or len(file_name) > 255
        or file_name in {".", ".."}
        or "/" in file_name
        or "\\" in file_name
        or PurePath(file_name).name != file_name
    ):
        raise TransientAttachmentError(
            "ATTACHMENT_EXTENSION_INVALID",
            422,
            "Attachment file name is invalid",
        )
    extension = PurePath(file_name).suffix.lower()
    if extension not in ALLOWED_MEDIA_TYPES:
        raise TransientAttachmentError(
            "ATTACHMENT_EXTENSION_INVALID",
            422,
            "Attachment extension is not allowed",
        )
    return file_name, extension


def _validate_payload(
    file_name: str,
    media_type: str,
    content: bytes,
) -> tuple[str, str, str]:
    normalized_name, extension = _safe_file_name(file_name)
    if not isinstance(media_type, str) or len(media_type.strip()) > 128:
        raise TransientAttachmentError(
            "ATTACHMENT_MIME_NOT_ALLOWED",
            422,
            "Attachment MIME type is invalid",
        )
    normalized_media_type = media_type.strip().lower()
    if normalized_media_type not in ALLOWED_MEDIA_TYPES[extension]:
        raise TransientAttachmentError(
            "ATTACHMENT_MIME_NOT_ALLOWED",
            422,
            "Attachment MIME type does not match its extension",
        )
    if not isinstance(content, bytes) or not content:
        raise TransientAttachmentError(
            "ATTACHMENT_EMPTY", 422, "Attachment content is empty"
        )
    if len(content) > attachment_max_size_bytes():
        raise TransientAttachmentError(
            "ATTACHMENT_TOO_LARGE",
            413,
            "Attachment exceeds the configured size limit",
        )
    return normalized_name, normalized_media_type, extension


def decode_attachment_content(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > attachment_max_encoded_length():
        raise TransientAttachmentError(
            "ATTACHMENT_TOO_LARGE",
            413,
            "Attachment exceeds the configured size limit",
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise TransientAttachmentError(
            "ATTACHMENT_CONTENT_INVALID",
            422,
            "Attachment content must be valid base64",
        ) from exc
    if len(decoded) > attachment_max_size_bytes():
        raise TransientAttachmentError(
            "ATTACHMENT_TOO_LARGE",
            413,
            "Attachment exceeds the configured size limit",
        )
    return decoded


def _row_to_record(row: aiosqlite.Row | dict[str, Any]) -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id=row["attachment_id"],
        tenant_id=row["tenant_id"],
        conversation_id=row["conversation_id"],
        business_user_id=row["business_user_id"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        extension=row["extension"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        expires_at=row["expires_at"],
        max_downloads=row["max_downloads"],
        download_count=row["download_count"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_attachment(db: aiosqlite.Connection, attachment_id: str):
    async with db.execute(
        "SELECT * FROM ext_transient_attachment WHERE attachment_id=?",
        (attachment_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _audit(
    db: aiosqlite.Connection,
    *,
    attachment_id: str,
    tenant_id: str,
    conversation_id: str,
    business_user_id: str,
    action: str,
    outcome: str,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """INSERT INTO ext_transient_attachment_audit
           (attachment_id, tenant_id, conversation_id, business_user_id,
            action, outcome, request_id, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            attachment_id,
            tenant_id,
            conversation_id,
            business_user_id,
            action,
            outcome,
            request_id,
            json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
            utc_now().isoformat(),
        ),
    )


async def _retry_storage_operation(
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    last_error: Exception | None = None
    attempts = attachment_retry_attempts()
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(0)
    assert last_error is not None
    raise last_error


class TransientAttachmentService:
    def __init__(
        self,
        db: aiosqlite.Connection,
        storage: AttachmentStorage | None = None,
        *,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.db = db
        self.storage = storage or _storage()
        self.now_fn = now_fn

    def _now(self) -> datetime:
        value = self.now_fn()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    async def create(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        business_user_id: str,
        file_name: str,
        media_type: str,
        content: bytes,
        request_id: str | None = None,
    ) -> tuple[AttachmentRecord, DownloadTicket]:
        file_name, media_type, extension = _validate_payload(
            file_name, media_type, content
        )
        bucket = _storage_bucket()
        if not bucket:
            raise TransientAttachmentError(
                "ATTACHMENT_STORAGE_UNAVAILABLE",
                503,
                "Attachment storage is temporarily unavailable",
                retryable=True,
            )

        now = self._now()
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=attachment_ttl_seconds())).isoformat()
        attachment_id = str(uuid.uuid4())
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
        conversation_hash = hashlib.sha256(conversation_id.encode()).hexdigest()[:16]
        object_key = (
            f"{_storage_prefix()}/{tenant_hash}/{conversation_hash}/"
            f"{uuid.uuid4().hex}{extension}"
        )
        digest = hashlib.sha256(content).hexdigest()
        max_downloads = attachment_max_downloads()

        await self.db.execute(
            """INSERT INTO ext_transient_attachment
               (attachment_id, tenant_id, conversation_id, business_user_id,
                object_bucket, object_key, file_name, media_type, extension,
                size_bytes, sha256, expires_at, max_downloads, download_count,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'uploading', ?, ?)""",
            (
                attachment_id,
                tenant_id,
                conversation_id,
                business_user_id,
                bucket,
                object_key,
                file_name,
                media_type,
                extension,
                len(content),
                digest,
                expires_at,
                max_downloads,
                created_at,
                created_at,
            ),
        )
        await _audit(
            self.db,
            attachment_id=attachment_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            business_user_id=business_user_id,
            action="create",
            outcome="accepted",
            request_id=request_id,
            metadata={
                "indexPolicy": "never",
                "mediaType": media_type,
                "sizeBytes": len(content),
            },
        )
        await self.db.commit()

        try:
            await _retry_storage_operation(
                lambda: self.storage.put_object(
                    bucket, object_key, content, media_type
                )
            )
        except SourceTooLarge as exc:
            await self._mark_upload_failed(
                attachment_id,
                tenant_id,
                conversation_id,
                business_user_id,
                "ATTACHMENT_TOO_LARGE",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_TOO_LARGE",
                413,
                "Attachment exceeds the configured size limit",
            ) from exc
        except Exception as exc:
            await self._mark_upload_failed(
                attachment_id,
                tenant_id,
                conversation_id,
                business_user_id,
                "ATTACHMENT_STORAGE_UNAVAILABLE",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_STORAGE_UNAVAILABLE",
                503,
                "Attachment storage is temporarily unavailable",
                retryable=True,
            ) from exc

        await self.db.execute(
            """UPDATE ext_transient_attachment
               SET status='active', upload_attempts=?, updated_at=?
               WHERE attachment_id=?""",
            (attachment_retry_attempts(), utc_now().isoformat(), attachment_id),
        )
        await self.db.commit()
        record = await self._record_or_not_found(attachment_id)
        ticket = await self.issue_download_ticket(
            attachment_id=attachment_id,
            tenant_id=tenant_id,
            business_user_id=business_user_id,
            request_id=request_id,
        )
        return record, ticket

    async def _mark_upload_failed(
        self,
        attachment_id: str,
        tenant_id: str,
        conversation_id: str,
        business_user_id: str,
        error_code: str,
        request_id: str | None,
    ) -> None:
        await self.db.execute(
            """UPDATE ext_transient_attachment
               SET status='upload_failed', upload_attempts=?,
                   last_error_code=?, last_error_message=?, updated_at=?
               WHERE attachment_id=?""",
            (
                attachment_retry_attempts(),
                error_code,
                "Object storage operation failed",
                utc_now().isoformat(),
                attachment_id,
            ),
        )
        await _audit(
            self.db,
            attachment_id=attachment_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            business_user_id=business_user_id,
            action="create",
            outcome="failed",
            request_id=request_id,
            metadata={
                "errorCode": error_code,
                "attempts": attachment_retry_attempts(),
            },
        )
        await self.db.commit()

    async def _record_or_not_found(self, attachment_id: str) -> AttachmentRecord:
        row = await _get_attachment(self.db, attachment_id)
        if not row:
            raise TransientAttachmentError(
                "ATTACHMENT_NOT_FOUND", 404, "Attachment not found"
            )
        return _row_to_record(row)

    async def _conversation_is_active(self, row: dict[str, Any]) -> bool:
        try:
            async with self.db.execute(
                """SELECT status FROM ext_v2_conversation
                   WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
                (
                    row["conversation_id"],
                    row["tenant_id"],
                    row["business_user_id"],
                ),
            ) as cursor:
                conversation = await cursor.fetchone()
        except aiosqlite.OperationalError as exc:
            raise TransientAttachmentError(
                "CONVERSATION_UNAVAILABLE",
                503,
                "Conversation history is temporarily unavailable",
                retryable=True,
            ) from exc
        if not conversation:
            return False
        status = conversation["status"]
        if status == "active":
            return True
        if status == "archived":
            return False
        raise TransientAttachmentError(
            "CONVERSATION_UNAVAILABLE",
            503,
            "Conversation history is temporarily unavailable",
            retryable=True,
        )

    async def issue_download_ticket(
        self,
        *,
        attachment_id: str,
        tenant_id: str,
        business_user_id: str,
        request_id: str | None = None,
    ) -> DownloadTicket:
        now = self._now()
        row = await _get_attachment(self.db, attachment_id)
        if not row:
            raise TransientAttachmentError(
                "ATTACHMENT_NOT_FOUND", 404, "Attachment not found"
            )
        if row["tenant_id"] != tenant_id or row["business_user_id"] != business_user_id:
            await self._record_denied(
                row,
                tenant_id,
                business_user_id,
                "ticket_issue",
                "owner_mismatch",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_FORBIDDEN", 403, "Attachment access is denied"
            )
        if not await self._conversation_is_active(row):
            await self._record_denied(
                row,
                tenant_id,
                business_user_id,
                "ticket_issue",
                "conversation_archived",
                request_id,
            )
            raise TransientAttachmentError(
                "CONVERSATION_ARCHIVED", 409, "Conversation is archived"
            )
        if row["status"] != "active" or _parse_timestamp(row["expires_at"]) <= now:
            await self._record_denied(
                row,
                tenant_id,
                business_user_id,
                "ticket_issue",
                "expired_or_deleted",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_EXPIRED", 410, "Attachment has expired"
            )
        if row["download_count"] >= row["max_downloads"]:
            await self._record_denied(
                row,
                tenant_id,
                business_user_id,
                "ticket_issue",
                "download_limit",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_DOWNLOAD_LIMIT",
                410,
                "Attachment download limit has been reached",
            )

        remaining = max(1, int((_parse_timestamp(row["expires_at"]) - now).total_seconds()))
        expires_at = (
            now + timedelta(seconds=min(attachment_ttl_seconds(), remaining))
        ).isoformat()
        token = secrets.token_urlsafe(32)
        await self.db.execute(
            """INSERT INTO ext_transient_attachment_ticket
               (ticket_hash, attachment_id, tenant_id, conversation_id,
                business_user_id, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                attachment_id,
                tenant_id,
                row["conversation_id"],
                business_user_id,
                expires_at,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await _audit(
            self.db,
            attachment_id=attachment_id,
            tenant_id=tenant_id,
            conversation_id=row["conversation_id"],
            business_user_id=business_user_id,
            action="ticket_issue",
            outcome="accepted",
            request_id=request_id,
            metadata={"expiresAt": expires_at},
        )
        await self.db.commit()
        return DownloadTicket(token, attachment_id, expires_at)

    async def _record_denied(
        self,
        row: dict[str, Any],
        tenant_id: str,
        business_user_id: str,
        action: str,
        reason: str,
        request_id: str | None,
    ) -> None:
        await _audit(
            self.db,
            attachment_id=row["attachment_id"],
            tenant_id=tenant_id,
            conversation_id=row["conversation_id"],
            business_user_id=business_user_id,
            action=action,
            outcome="denied",
            request_id=request_id,
            metadata={"reason": reason},
        )
        await self.db.commit()

    async def download(
        self,
        *,
        attachment_id: str,
        token: str,
        principal: UserPrincipal | None = None,
        request_id: str | None = None,
    ) -> DownloadedAttachment:
        if not token or len(token) > 256:
            raise TransientAttachmentError(
                "ATTACHMENT_TICKET_INVALID", 404, "Download ticket is invalid"
            )
        ticket_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await self._claim_ticket(
            attachment_id,
            ticket_hash,
            principal,
            self._now(),
            request_id,
        )
        claim_time = row["claimed_at"]
        try:
            source = await _retry_storage_operation(
                lambda: self.storage.fetch(
                    row["object_bucket"], row["object_key"], row["sha256"]
                )
            )
            media_type_matches = (
                not source.media_type
                or source.media_type == "application/octet-stream"
                or source.media_type.lower() == row["media_type"].lower()
            )
            if (
                source.size != row["size_bytes"]
                or len(source.content) != row["size_bytes"]
                or hashlib.sha256(source.content).hexdigest() != row["sha256"]
                or not media_type_matches
            ):
                raise TransientAttachmentError(
                    "ATTACHMENT_STORAGE_CORRUPT",
                    502,
                    "Attachment storage content failed validation",
                    retryable=True,
                )
        except TransientAttachmentError:
            await self._release_claim(row, claim_time, request_id, "ATTACHMENT_STORAGE_CORRUPT")
            raise
        except Exception as exc:
            await self._release_claim(
                row, claim_time, request_id, "ATTACHMENT_STORAGE_UNAVAILABLE"
            )
            raise TransientAttachmentError(
                "ATTACHMENT_STORAGE_UNAVAILABLE",
                503,
                "Attachment storage is temporarily unavailable",
                retryable=True,
            ) from exc

        try:
            record = await self._complete_claim(row, claim_time, request_id)
        except Exception as exc:
            await self._release_claim(
                row, claim_time, request_id, "ATTACHMENT_DOWNLOAD_RETRYABLE"
            )
            raise TransientAttachmentError(
                "ATTACHMENT_STORAGE_UNAVAILABLE",
                503,
                "Attachment download could not be finalized",
                retryable=True,
            ) from exc
        return DownloadedAttachment(record, source.content)

    async def _claim_ticket(
        self,
        attachment_id: str,
        ticket_hash: str,
        principal: UserPrincipal | None,
        now: datetime,
        request_id: str | None,
    ):
        await self.db.execute("BEGIN IMMEDIATE")
        async with self.db.execute(
            """SELECT a.*, t.ticket_hash AS ticket_hash,
                      t.expires_at AS ticket_expires_at,
                      t.claimed_at, t.claim_expires_at
               FROM ext_transient_attachment a
               JOIN ext_transient_attachment_ticket t
                 ON t.attachment_id=a.attachment_id
              WHERE a.attachment_id=? AND t.ticket_hash=?""",
            (attachment_id, ticket_hash),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await self.db.rollback()
            existing = await _get_attachment(self.db, attachment_id)
            if existing:
                await self._record_denied(
                    existing,
                    existing["tenant_id"],
                    principal.business_user_id if principal else "unknown",
                    "download",
                    "invalid_ticket",
                    request_id,
                )
            raise TransientAttachmentError(
                "ATTACHMENT_TICKET_INVALID", 404, "Download ticket is invalid"
            )

        if principal and (
            principal.tenant_id != row["tenant_id"]
            or principal.business_user_id != row["business_user_id"]
            or not {"ask", "admin"}.intersection(principal.capabilities)
        ):
            await self.db.rollback()
            await self._record_denied(
                row,
                principal.tenant_id,
                principal.business_user_id,
                "download",
                "principal_mismatch",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_FORBIDDEN", 403, "Attachment access is denied"
            )

        try:
            conversation_active = await self._conversation_is_active(row)
        except TransientAttachmentError:
            try:
                await self.db.rollback()
            except Exception:
                pass
            raise
        if not conversation_active:
            await self.db.rollback()
            await self._record_denied(
                row,
                row["tenant_id"],
                principal.business_user_id if principal else row["business_user_id"],
                "download",
                "conversation_archived",
                request_id,
            )
            raise TransientAttachmentError(
                "CONVERSATION_ARCHIVED", 409, "Conversation is archived"
            )

        expired = (
            row["status"] != "active"
            or _parse_timestamp(row["ticket_expires_at"]) <= now
            or _parse_timestamp(row["expires_at"]) <= now
        )
        if expired:
            await self.db.rollback()
            await self._record_denied(
                row,
                row["tenant_id"],
                principal.business_user_id if principal else row["business_user_id"],
                "download",
                "expired_or_deleted",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_EXPIRED", 410, "Attachment has expired"
            )
        if row["download_count"] >= row["max_downloads"]:
            await self.db.rollback()
            await self._record_denied(
                row,
                row["tenant_id"],
                principal.business_user_id if principal else row["business_user_id"],
                "download",
                "download_limit",
                request_id,
            )
            raise TransientAttachmentError(
                "ATTACHMENT_DOWNLOAD_LIMIT",
                410,
                "Attachment download limit has been reached",
            )
        if (
            row["claimed_at"]
            and row["claim_expires_at"]
            and _parse_timestamp(row["claim_expires_at"]) > now
        ):
            await self.db.rollback()
            raise TransientAttachmentError(
                "ATTACHMENT_TICKET_INVALID", 404, "Download ticket is invalid"
            )

        claim_time = now.isoformat()
        claim_expires = (
            now
            + timedelta(
                seconds=_env_int("ENTERPRISE_ATTACHMENT_CLAIM_SECONDS", 60)
            )
        ).isoformat()
        cursor = await self.db.execute(
            """UPDATE ext_transient_attachment_ticket
               SET claimed_at=?, claim_expires_at=?, updated_at=?
               WHERE ticket_hash=? AND consumed_at IS NULL
                 AND (claimed_at IS NULL OR claim_expires_at<=?)""",
            (claim_time, claim_expires, claim_time, ticket_hash, now.isoformat()),
        )
        if cursor.rowcount != 1:
            await self.db.rollback()
            raise TransientAttachmentError(
                "ATTACHMENT_TICKET_INVALID", 404, "Download ticket is invalid"
            )
        await self.db.commit()
        return dict(row) | {"claimed_at": claim_time}

    async def _release_claim(
        self,
        row: dict[str, Any],
        claim_time: str,
        request_id: str | None,
        error_code: str,
    ) -> None:
        await self.db.execute(
            """UPDATE ext_transient_attachment_ticket
               SET claimed_at=NULL, claim_expires_at=NULL,
                   download_attempts=download_attempts+1,
                   last_error_code=?, updated_at=?
               WHERE ticket_hash=? AND claimed_at=? AND consumed_at IS NULL""",
            (error_code, utc_now().isoformat(), row["ticket_hash"], claim_time),
        )
        await _audit(
            self.db,
            attachment_id=row["attachment_id"],
            tenant_id=row["tenant_id"],
            conversation_id=row["conversation_id"],
            business_user_id=row["business_user_id"],
            action="download",
            outcome="failed",
            request_id=request_id,
            metadata={"errorCode": error_code},
        )
        await self.db.commit()

    async def _complete_claim(
        self,
        row: dict[str, Any],
        claim_time: str,
        request_id: str | None,
    ) -> AttachmentRecord:
        now = utc_now().isoformat()
        await self.db.execute("BEGIN IMMEDIATE")
        cursor = await self.db.execute(
            """UPDATE ext_transient_attachment_ticket
               SET consumed_at=?, claimed_at=NULL, claim_expires_at=NULL,
                   updated_at=?
               WHERE ticket_hash=? AND claimed_at=? AND consumed_at IS NULL""",
            (now, now, row["ticket_hash"], claim_time),
        )
        if cursor.rowcount != 1:
            await self.db.rollback()
            raise RuntimeError("attachment ticket claim was lost")
        await self.db.execute(
            """UPDATE ext_transient_attachment
               SET download_count=download_count+1, updated_at=?
               WHERE attachment_id=? AND status='active'""",
            (now, row["attachment_id"]),
        )
        await _audit(
            self.db,
            attachment_id=row["attachment_id"],
            tenant_id=row["tenant_id"],
            conversation_id=row["conversation_id"],
            business_user_id=row["business_user_id"],
            action="download",
            outcome="accepted",
            request_id=request_id,
            metadata={"sizeBytes": row["size_bytes"]},
        )
        await self.db.commit()
        return await self._record_or_not_found(row["attachment_id"])

    async def cleanup_expired(self, *, limit: int = 100) -> dict[str, int]:
        now = self._now()
        now_iso = now.isoformat()
        async with self.db.execute(
            """SELECT * FROM ext_transient_attachment
               WHERE (expires_at<=? AND status IN ('active', 'uploading', 'upload_failed'))
                  OR (status='delete_retry' AND
                      (next_retry_at IS NULL OR next_retry_at<=?))
               ORDER BY expires_at ASC LIMIT ?""",
            (now_iso, now_iso, max(1, min(limit, 1000))),
        ) as cursor:
            rows = await cursor.fetchall()

        deleted = 0
        failed = 0
        for row in rows:
            if row["status"] in {"active", "uploading", "upload_failed"}:
                await self.db.execute(
                    """UPDATE ext_transient_attachment
                       SET status='expired', updated_at=?
                       WHERE attachment_id=?""",
                    (now_iso, row["attachment_id"]),
                )
                await _audit(
                    self.db,
                    attachment_id=row["attachment_id"],
                    tenant_id=row["tenant_id"],
                    conversation_id=row["conversation_id"],
                    business_user_id=row["business_user_id"],
                    action="expire",
                    outcome="accepted",
                    metadata={"expiresAt": row["expires_at"]},
                )
                await self.db.commit()
            try:
                await _retry_storage_operation(
                    lambda: self.storage.delete_object(
                        row["object_bucket"], row["object_key"]
                    )
                )
            except Exception:
                failed += 1
                attempts = row["delete_attempts"] + attachment_retry_attempts()
                await self.db.execute(
                    """UPDATE ext_transient_attachment
                       SET status='delete_retry', delete_attempts=?,
                           next_retry_at=?, last_error_code=?,
                           last_error_message=?, updated_at=?
                       WHERE attachment_id=?""",
                    (
                        attempts,
                        (now + timedelta(seconds=_retry_delay_seconds(attempts))).isoformat(),
                        "ATTACHMENT_CLEANUP_RETRY",
                        "Object storage cleanup failed",
                        utc_now().isoformat(),
                        row["attachment_id"],
                    ),
                )
                await _audit(
                    self.db,
                    attachment_id=row["attachment_id"],
                    tenant_id=row["tenant_id"],
                    conversation_id=row["conversation_id"],
                    business_user_id=row["business_user_id"],
                    action="cleanup",
                    outcome="failed",
                    metadata={
                        "errorCode": "ATTACHMENT_CLEANUP_RETRY",
                        "attempts": attempts,
                    },
                )
                await self.db.commit()
                continue

            deleted += 1
            await self.db.execute(
                """UPDATE ext_transient_attachment
                   SET status='deleted', deleted_at=?, next_retry_at=NULL,
                       last_error_code=NULL, last_error_message=NULL,
                       updated_at=?
                   WHERE attachment_id=?""",
                (now_iso, now_iso, row["attachment_id"]),
            )
            await self.db.execute(
                "DELETE FROM ext_transient_attachment_ticket WHERE attachment_id=?",
                (row["attachment_id"],),
            )
            await _audit(
                self.db,
                attachment_id=row["attachment_id"],
                tenant_id=row["tenant_id"],
                conversation_id=row["conversation_id"],
                business_user_id=row["business_user_id"],
                action="cleanup",
                outcome="accepted",
                metadata={"deleted": True},
            )
            await self.db.commit()

        await self.db.execute(
            """DELETE FROM ext_transient_attachment_ticket
               WHERE consumed_at IS NOT NULL OR expires_at<=?""",
            (now_iso,),
        )
        await self.db.commit()
        return {"examined": len(rows), "deleted": deleted, "failed": failed}


class TransientAttachmentCleanupWorker:
    def __init__(self, service: TransientAttachmentService) -> None:
        self.service = service

    async def run_once(self) -> dict[str, int]:
        return await self.service.cleanup_expired()

    async def run_forever(self, interval_seconds: int) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Transient attachment cleanup failed")
            await asyncio.sleep(interval_seconds)


async def get_db() -> aiosqlite.Connection:
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    value = dependency()
    return await value if asyncio.iscoroutine(value) else value


async def get_storage() -> AttachmentStorage:
    return _storage()


async def optional_user_principal(request: Request) -> UserPrincipal | None:
    """Use the ticket as a bearer capability, binding a supplied JWT when present."""
    header = request.headers.get("Authorization", "").strip()
    if not header:
        return None
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise UserAuthError(401, "AUTH_TOKEN_INVALID", "Authentication token is invalid")
    override = request.app.dependency_overrides.get(require_user_principal)
    if override:
        value = override()
        return await value if inspect.isawaitable(value) else value
    return await require_user_principal(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token.strip()),
    )


router = APIRouter(prefix="/enterprise/api/v2", tags=["transient-attachments"])


def _error(exc: TransientAttachmentError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "requestId": request_id,
            "retryable": exc.retryable,
        },
    )


def _not_implemented(request_id: str) -> JSONResponse:
    return _error(
        TransientAttachmentError(
            "ATTACHMENT_NOT_IMPLEMENTED",
            501,
            ATTACHMENT_NOT_IMPLEMENTED_MESSAGE,
        ),
        request_id,
    )


class TransientAttachmentBodyLimitMiddleware:
    """Gate transient attachment routes and bound creates before JSON parsing."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    @staticmethod
    def _route_kind(scope: dict[str, Any]) -> str | None:
        method = scope.get("method")
        parts = str(scope.get("path", "")).split("/")
        if (
            method == "POST"
            and len(parts) == 7
            and parts[:5] == ["", "enterprise", "api", "v2", "conversations"]
            and bool(parts[5])
            and parts[6] == "attachments"
        ):
            return "create"
        if (
            method == "POST"
            and len(parts) == 7
            and parts[:5] == ["", "enterprise", "api", "v2", "attachments"]
            and bool(parts[5])
            and parts[6] == "ticket"
        ):
            return "ticket"
        if (
            method == "GET"
            and len(parts) == 8
            and parts[:5] == ["", "enterprise", "api", "v2", "attachments"]
            and bool(parts[5])
            and parts[6] == "download"
            and bool(parts[7])
        ):
            return "download"
        return None

    @staticmethod
    def _declared_length(scope: dict[str, Any]) -> int | None:
        lengths: list[int] = []
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                lengths.append(parsed)
        return max(lengths) if lengths else None

    async def __call__(self, scope, receive, send):
        route_kind = self._route_kind(scope) if scope.get("type") == "http" else None
        if route_kind is None:
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        if not config.transient_attachments_enabled:
            response = _not_implemented(request_id)
            await response(scope, receive, send)
            return

        if route_kind != "create":
            await self.app(scope, receive, send)
            return

        limit = attachment_max_request_body_bytes()
        declared_length = self._declared_length(scope)
        if declared_length is not None and declared_length > limit:
            response = _error(
                TransientAttachmentError(
                    "ATTACHMENT_TOO_LARGE",
                    413,
                    "Attachment exceeds the configured size limit",
                ),
                request_id,
            )
            await response(scope, receive, send)
            return

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                await self.app(scope, receive, send)
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > limit:
                response = _error(
                    TransientAttachmentError(
                        "ATTACHMENT_TOO_LARGE",
                        413,
                        "Attachment exceeds the configured size limit",
                    ),
                    request_id,
                )
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        chunk_index = 0

        async def replay_receive():
            nonlocal chunk_index
            if chunk_index < len(chunks):
                chunk = chunks[chunk_index]
                chunk_index += 1
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": chunk_index < len(chunks),
                }
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def _safe_download_name(file_name: str) -> str:
    return PurePath(file_name).name.replace('"', "_")


def _attachment_payload(
    request: Request,
    record: AttachmentRecord,
    ticket: DownloadTicket,
) -> dict[str, Any]:
    return {
        "attachmentId": record.attachment_id,
        "conversationId": record.conversation_id,
        "fileName": record.file_name,
        "mediaType": record.media_type,
        "sizeBytes": record.size_bytes,
        "sha256": record.sha256,
        "expiresAt": record.expires_at,
        "indexPolicy": "never",
        "maxDownloads": record.max_downloads,
        "downloadCount": record.download_count,
        "downloadUrl": str(
            request.url_for(
                "download_transient_attachment",
                attachment_id=record.attachment_id,
                ticket=ticket.token,
            )
        ),
        "ticketExpiresAt": ticket.expires_at,
    }


@router.post(
    "/conversations/{conversation_id}/attachments",
    status_code=201,
    include_in_schema=False,
)
async def create_transient_attachment(
    conversation_id: str,
    req: CreateAttachmentRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    storage: AttachmentStorage = Depends(get_storage),
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    request_id = str(uuid.uuid4())
    await v2_store.ensure_schema(db)
    conversation = await v2_store.get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not conversation:
        return JSONResponse(
            status_code=404,
            content={
                "code": "CONVERSATION_NOT_FOUND",
                "message": "Conversation not found",
                "requestId": request_id,
            },
        )
    if conversation["status"] == "archived":
        return JSONResponse(
            status_code=409,
            content={
                "code": "CONVERSATION_ARCHIVED",
                "message": "Conversation is archived",
                "requestId": request_id,
            },
        )
    try:
        service = TransientAttachmentService(db, storage)
        record, ticket = await service.create(
            tenant_id=principal.tenant_id,
            conversation_id=conversation_id,
            business_user_id=principal.business_user_id,
            file_name=req.fileName,
            media_type=req.mediaType,
            content=decode_attachment_content(req.content),
            request_id=request_id,
        )
    except TransientAttachmentError as exc:
        return _error(exc, request_id)
    return _attachment_payload(request, record, ticket)


@router.post("/attachments/{attachment_id}/ticket", include_in_schema=False)
async def issue_transient_attachment_ticket(
    attachment_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    request_id = str(uuid.uuid4())
    try:
        await v2_store.ensure_schema(db)
        service = TransientAttachmentService(db)
        record = await service._record_or_not_found(attachment_id)
        conversation = await v2_store.get_conversation(
            db,
            conversation_id=record.conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
        )
        if not conversation:
            raise TransientAttachmentError(
                "ATTACHMENT_FORBIDDEN", 403, "Attachment access is denied"
            )
        ticket = await service.issue_download_ticket(
            attachment_id=attachment_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            request_id=request_id,
        )
    except TransientAttachmentError as exc:
        return _error(exc, request_id)
    return _attachment_payload(request, record, ticket)


@router.get(
    "/attachments/{attachment_id}/download/{ticket}",
    include_in_schema=False,
    name="download_transient_attachment",
)
async def download_transient_attachment(
    attachment_id: str,
    ticket: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    storage: AttachmentStorage = Depends(get_storage),
    principal: UserPrincipal | None = Depends(optional_user_principal),
):
    request_id = str(uuid.uuid4())
    try:
        downloaded = await TransientAttachmentService(db, storage).download(
            attachment_id=attachment_id,
            token=ticket,
            principal=principal,
            request_id=request_id,
        )
    except TransientAttachmentError as exc:
        return _error(exc, request_id)
    return Response(
        content=downloaded.content,
        media_type=downloaded.record.media_type,
        headers={
            "Content-Length": str(downloaded.record.size_bytes),
            "Content-Disposition": (
                f'attachment; filename="{_safe_download_name(downloaded.record.file_name)}"'
            ),
            "Cache-Control": "private, no-store",
            "ETag": downloaded.record.sha256,
            "X-Attachment-Id": downloaded.record.attachment_id,
        },
    )


__all__ = [
    "AttachmentRecord",
    "AttachmentStorage",
    "CreateAttachmentRequest",
    "DownloadTicket",
    "TransientAttachmentBodyLimitMiddleware",
    "TransientAttachmentCleanupWorker",
    "TransientAttachmentError",
    "TransientAttachmentService",
    "attachment_cleanup_interval_seconds",
    "attachment_max_encoded_length",
    "attachment_max_request_body_bytes",
    "ensure_attachment_schema",
    "get_db",
    "get_storage",
    "optional_user_principal",
    "router",
    "utc_now",
]
