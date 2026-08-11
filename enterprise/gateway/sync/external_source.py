"""Read-only FILE_SHARE access and short-lived internal source tickets.

The enterprise file server remains the authority for source PDFs.  This module
stores only an opaque ticket and source coordinates in the Enterprise SQLite
database; it never stores source bytes.  The ticket endpoint is intended for
the RAGFlow worker, not for browsers or public clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from enterprise.gateway.sync.source_adapter import (
    SourceAdapter,
    SourceFetchError,
    SourceFile,
    SourceHashMismatch,
    SourceTooLarge,
)


class FileShareConfigurationError(SourceFetchError):
    """The configured read-only root cannot be used safely."""


class SourceTicketUnavailable(SourceFetchError):
    """An opaque ticket is missing, expired, replayed, or inconsistent."""


@dataclass(frozen=True)
class SourceStat:
    size: int
    modified_ns: int
    etag: str


@dataclass(frozen=True)
class SourceTicket:
    token: str
    size: int
    modified_ns: int
    etag: str
    expires_at: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ticket_ttl_seconds() -> int:
    try:
        return max(30, int(os.getenv("ENTERPRISE_SOURCE_TICKET_TTL_SECONDS", "300")))
    except ValueError:
        return 300


def _max_size_bytes() -> int:
    try:
        return max(1, int(os.getenv("ENTERPRISE_FILE_SHARE_MAX_SIZE_MB", "512"))) * 1024 * 1024
    except ValueError:
        return 512 * 1024 * 1024


def _configured_roots() -> dict[str, Path]:
    """Load deployment-provided read-only mount roots.

    Production uses ``ENTERPRISE_FILE_SHARE_ROOTS`` as a JSON object mapping a
    stable root id to a mounted directory.  The single-root form is retained
    for small deployments and tests.  SMB credentials never belong here: the
    host/container mount is the credential boundary.
    """

    raw = os.getenv("ENTERPRISE_FILE_SHARE_ROOTS", "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FileShareConfigurationError("Invalid FILE_SHARE root registry") from exc
        if not isinstance(value, dict):
            raise FileShareConfigurationError("FILE_SHARE root registry must be an object")
        roots = {
            str(root_id): Path(str(root_path))
            for root_id, root_path in value.items()
            if str(root_id).strip() and str(root_path).strip()
        }
        if roots:
            return roots
    root = os.getenv("ENTERPRISE_FILE_SHARE_ROOT", "").strip()
    root_id = os.getenv("ENTERPRISE_FILE_SHARE_ROOT_ID", "default").strip() or "default"
    return {root_id: Path(root)} if root else {}


def _safe_relative_path(relative_path: str) -> Path:
    normalized = str(relative_path or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ":" in normalized
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FileShareConfigurationError("relativePath must stay below the configured root")
    return Path(*pure.parts)


class FileShareSourceAdapter(SourceAdapter):
    """Safe reader for a host-mounted, read-only enterprise file share."""

    def __init__(
        self,
        roots: dict[str, str | Path] | None = None,
        max_size_bytes: int | None = None,
    ) -> None:
        self._roots = {
            str(root_id): Path(root_path)
            for root_id, root_path in (roots or {}).items()
        }
        self.max_size_bytes = max_size_bytes or _max_size_bytes()

    def _root_registry(self) -> dict[str, Path]:
        return self._roots or _configured_roots()

    def resolve_path(self, storage_root_id: str, relative_path: str) -> Path:
        roots = self._root_registry()
        root = roots.get(storage_root_id)
        if root is None:
            raise FileShareConfigurationError("Unknown FILE_SHARE storage root")
        safe_relative = _safe_relative_path(relative_path)
        root_resolved = root.resolve(strict=False)
        candidate = (root_resolved / safe_relative).resolve(strict=False)
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise FileShareConfigurationError("FILE_SHARE path escapes configured root") from exc
        if not candidate.is_file():
            raise SourceFetchError("FILE_SHARE source file was not found")
        return candidate

    def stat_source(self, storage_root_id: str, relative_path: str) -> SourceStat:
        path = self.resolve_path(storage_root_id, relative_path)
        stat = path.stat()
        if stat.st_size > self.max_size_bytes:
            raise SourceTooLarge(f"FILE_SHARE source exceeds {self.max_size_bytes} bytes")
        return SourceStat(
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            etag=f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"',
        )

    async def fetch(
        self,
        bucket: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> SourceFile:
        """Read a FILE_SHARE source for direct tests or citation workers.

        Normal ingestion uses :meth:`issue_ticket` so Gateway does not read the
        same source once for hashing and again for upload.
        """

        stat = self.stat_source(bucket, object_key)
        path = self.resolve_path(bucket, object_key)
        content = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise SourceHashMismatch("FILE_SHARE source SHA256 does not match the version contract")
        return SourceFile(
            content=content,
            file_name=path.name,
            media_type="application/pdf",
            size=stat.size,
            sha256=digest,
        )

    async def issue_ticket(
        self,
        db: aiosqlite.Connection,
        *,
        tenant_id: str,
        source_system: str,
        external_document_id: str,
        source_version_id: str,
        storage_root_id: str,
        relative_path: str,
        file_name: str,
        media_type: str,
        expected_sha256: str,
    ) -> SourceTicket:
        stat = self.stat_source(storage_root_id, relative_path)
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires_at = now + timedelta(seconds=_ticket_ttl_seconds())
        await purge_source_tickets(db, now=now)
        await db.execute(
            """INSERT INTO ext_source_ticket
               (ticket_hash, tenant_id, source_system, external_document_id,
                source_version_id, storage_root_id, relative_path, file_name,
                media_type, expected_sha256, source_size, source_modified_ns,
                source_etag, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                tenant_id,
                source_system,
                external_document_id,
                source_version_id,
                storage_root_id,
                relative_path,
                file_name,
                media_type,
                expected_sha256.lower(),
                stat.size,
                stat.modified_ns,
                stat.etag,
                expires_at.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await db.commit()
        return SourceTicket(
            token=token,
            size=stat.size,
            modified_ns=stat.modified_ns,
            etag=stat.etag,
            expires_at=expires_at.isoformat(),
        )


async def purge_source_tickets(
    db: aiosqlite.Connection,
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Bound metadata growth from expired or already consumed tickets."""
    cutoff = (now or utc_now()).isoformat()
    cursor = await db.execute(
        """DELETE FROM ext_source_ticket
           WHERE id IN (
             SELECT id FROM ext_source_ticket
             WHERE expires_at<=? OR consumed_at IS NOT NULL
             ORDER BY id LIMIT ?
           )""",
        (cutoff, max(1, min(limit, 10000))),
    )
    await db.commit()
    return cursor.rowcount


async def consume_source_ticket(
    db: aiosqlite.Connection,
    token: str,
) -> dict | None:
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = utc_now().isoformat()
    try:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            """SELECT * FROM ext_source_ticket
               WHERE ticket_hash=? AND consumed_at IS NULL AND expires_at>?""",
            (token_hash, now),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return None
        await db.execute(
            """UPDATE ext_source_ticket SET consumed_at=?, updated_at=?
               WHERE ticket_hash=? AND consumed_at IS NULL""",
            (now, now, token_hash),
        )
        await db.commit()
        return dict(row)
    except Exception:
        await db.rollback()
        raise


async def _db_dependency() -> aiosqlite.Connection:
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024):
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


router = APIRouter(prefix="/enterprise/internal", tags=["internal-source"])


@router.get("/source-tickets/{token}", include_in_schema=False)
async def download_source_ticket(
    token: str,
    request: Request,
    db: aiosqlite.Connection = Depends(_db_dependency),
):
    internal_key = os.getenv("TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY", "")
    if not internal_key and os.getenv("ENTERPRISE_TEST_MODE") != "1":
        return JSONResponse(status_code=404, content={"code": "SOURCE_TICKET_NOT_FOUND"})
    if internal_key and request.headers.get("X-Internal-Source-Key") != internal_key:
        return JSONResponse(status_code=404, content={"code": "SOURCE_TICKET_NOT_FOUND"})
    ticket = await consume_source_ticket(db, token)
    if ticket is None:
        return JSONResponse(status_code=404, content={"code": "SOURCE_TICKET_NOT_FOUND"})

    provider = FileShareSourceAdapter()
    try:
        current = provider.stat_source(ticket["storage_root_id"], ticket["relative_path"])
        if (
            current.size != ticket["source_size"]
            or current.modified_ns != ticket["source_modified_ns"]
        ):
            return JSONResponse(status_code=409, content={"code": "DOCUMENT_SOURCE_CHANGED"})
        path = provider.resolve_path(ticket["storage_root_id"], ticket["relative_path"])
    except SourceFetchError:
        return JSONResponse(status_code=404, content={"code": "DOCUMENT_SOURCE_NOT_FOUND"})

    safe_name = Path(ticket["file_name"]).name.replace('"', "_")
    encoded_name = quote(safe_name, safe="")
    headers = {
        "Content-Length": str(current.size),
        "Content-Type": ticket["media_type"] or "application/pdf",
        # HTTP headers are Latin-1 in Starlette; keep an ASCII fallback and
        # preserve the original Unicode name through RFC 5987 filename*.
        "Content-Disposition": (
            f'attachment; filename="source.pdf"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "ETag": current.etag,
        "X-Source-SHA256": ticket["expected_sha256"],
        "X-Source-Version-Id": ticket["source_version_id"],
        "Cache-Control": "no-store",
    }
    return StreamingResponse(_file_chunks(path), headers=headers, media_type=headers["Content-Type"])
