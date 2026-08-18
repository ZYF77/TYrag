"""Short-lived hashed tickets for unified citation file downloads."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import aiosqlite

from enterprise.gateway.auth.user_principal import UserPrincipal


logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 900
DEFAULT_MAX_DOWNLOADS = 10
PUBLIC_CITATION_KEYS = (
    "citationId",
    "sourceType",
    "title",
    "externalDocumentId",
    "sourceVersionId",
    "pageNo",
    "bbox",
    "assetId",
    "excerpt",
    "recordType",
    "recordId",
    "downloadUrl",
    "downloadExpiresAt",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_citation_file_ticket (
    ticket_hash TEXT PRIMARY KEY,
    citation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    image_id TEXT,
    principal_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_downloads INTEGER NOT NULL,
    download_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ext_citation_file_ticket_expiry
    ON ext_citation_file_ticket(expires_at, citation_id);
"""

ImageFetcher = Callable[[str], Awaitable[tuple[bytes, str] | None]]
_image_fetcher: ImageFetcher | None = None


class CitationFileError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CitationFileTicket:
    token: str
    expires_at: str
    kind: str


def citation_file_ttl_seconds() -> int:
    raw = os.getenv("ENTERPRISE_CITATION_FILE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))
    try:
        return max(30, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def citation_file_max_downloads() -> int:
    raw = os.getenv("ENTERPRISE_CITATION_FILE_MAX_DOWNLOADS", str(DEFAULT_MAX_DOWNLOADS))
    try:
        return max(1, min(int(raw), 100))
    except ValueError:
        return DEFAULT_MAX_DOWNLOADS


def set_citation_image_fetcher(fetcher: ImageFetcher | None) -> None:
    global _image_fetcher
    _image_fetcher = fetcher


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def citation_file_kind(citation: dict) -> str:
    image_id = str(citation.get("imageId") or "").strip()
    return "crop" if image_id else "original"


async def ensure_citation_file_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    await db.commit()


async def issue_citation_file_ticket(
    db: aiosqlite.Connection,
    *,
    citation: dict,
    principal: UserPrincipal,
) -> CitationFileTicket:
    await ensure_citation_file_schema(db)
    now = utc_now()
    await db.execute(
        "DELETE FROM ext_citation_file_ticket WHERE expires_at<=?",
        (now.isoformat(),),
    )
    expires_at = (now + timedelta(seconds=citation_file_ttl_seconds())).isoformat()
    token = secrets.token_urlsafe(32)
    kind = citation_file_kind(citation)
    principal_json = json.dumps(
        {
            "subject": principal.subject,
            "department_ids": list(principal.department_ids),
            "group_ids": list(principal.group_ids),
            "security_level": principal.security_level,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    await db.execute(
        """INSERT INTO ext_citation_file_ticket
           (ticket_hash, citation_id, tenant_id, business_user_id, kind,
            image_id, principal_json, expires_at, max_downloads, download_count,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            citation["citationId"],
            principal.tenant_id,
            principal.business_user_id,
            kind,
            str(citation.get("imageId") or "") or None,
            principal_json,
            expires_at,
            citation_file_max_downloads(),
            now.isoformat(),
        ),
    )
    await db.commit()
    return CitationFileTicket(token=token, expires_at=expires_at, kind=kind)


def public_citation(
    citation: dict,
    ticket: CitationFileTicket,
    download_url: str,
) -> dict:
    return {
        "citationId": citation["citationId"],
        "sourceType": citation.get("sourceType", "document"),
        "title": citation.get("title") or "",
        "externalDocumentId": citation.get("externalDocumentId")
        or citation.get("documentId"),
        "sourceVersionId": citation.get("sourceVersionId")
        or citation.get("versionId"),
        "pageNo": citation.get("pageNo"),
        "bbox": citation.get("bbox"),
        "assetId": citation.get("assetId"),
        "excerpt": citation.get("excerpt"),
        "recordType": citation.get("recordType"),
        "recordId": citation.get("recordId"),
        "downloadUrl": download_url,
        "downloadExpiresAt": ticket.expires_at,
    }


def principal_from_ticket(row: aiosqlite.Row) -> UserPrincipal:
    try:
        payload = json.loads(row["principal_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return UserPrincipal(
        tenant_id=row["tenant_id"],
        business_user_id=row["business_user_id"],
        subject=str(payload.get("subject") or row["business_user_id"]),
        department_ids=tuple(payload.get("department_ids") or ()),
        group_ids=tuple(payload.get("group_ids") or ()),
        security_level=int(payload.get("security_level") or 0),
        mapping_status="active",
        capabilities=("view_citations",),
    )


def _not_found() -> CitationFileError:
    return CitationFileError(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")


async def claim_citation_file_ticket(
    db: aiosqlite.Connection,
    citation_id: str,
    token: str,
) -> dict:
    await ensure_citation_file_schema(db)
    ticket_hash = hashlib.sha256(token.encode()).hexdigest()
    now = utc_now()
    async with db.execute(
        """SELECT citation_id, tenant_id, business_user_id, kind, image_id,
                  principal_json, expires_at, max_downloads, download_count
           FROM ext_citation_file_ticket
           WHERE ticket_hash=? AND citation_id=?""",
        (ticket_hash, citation_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise _not_found()
    if _parse_timestamp(row["expires_at"]) <= now:
        raise _not_found()
    if int(row["download_count"]) >= int(row["max_downloads"]):
        raise _not_found()
    cursor = await db.execute(
        """UPDATE ext_citation_file_ticket
           SET download_count=download_count+1
           WHERE ticket_hash=? AND citation_id=?
             AND download_count < max_downloads
             AND expires_at>?""",
        (ticket_hash, citation_id, now.isoformat()),
    )
    if cursor.rowcount != 1:
        raise _not_found()
    await db.commit()
    return dict(row)


async def fetch_citation_image(image_id: str) -> tuple[bytes, str] | None:
    if not image_id or _image_fetcher is None:
        return None
    try:
        return await _image_fetcher(image_id)
    except Exception:
        logger.warning("citation crop fetch failed image_id=%s", image_id)
        return None
