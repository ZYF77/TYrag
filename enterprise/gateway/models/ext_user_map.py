"""ExtUserMap persistence.

Uses the same aiosqlite Repository pattern as WP-02A ext_document_map.
SQLite is a local development default; production PostgreSQL migration
is documented in docs/04-SSO-RBAC-ACL.md.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

CREATE_EXT_USER_MAP = """
CREATE TABLE IF NOT EXISTS ext_user_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT,
    business_subject TEXT NOT NULL,
    ragflow_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    mapping_strategy TEXT NOT NULL DEFAULT 'B',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    last_login_at TEXT,
    UNIQUE(tenant_id, business_subject)
);
"""


@dataclass
class ExtUserMap:
    tenant_id: str
    business_subject: str
    business_user_id: str | None = None
    ragflow_user_id: str | None = None
    status: str = "active"
    mapping_strategy: str = "B"
    last_login_at: str | None = None
    id: int | None = None

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "business_user_id": self.business_user_id,
            "business_subject": self.business_subject,
            "ragflow_user_id": self.ragflow_user_id,
            "status": self.status,
            "mapping_strategy": self.mapping_strategy,
            "last_login_at": self.last_login_at,
        }


class ExtUserMapRepo:
    """Repository for ext_user_map with persistent connection."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.environ.get(
            "ENTERPRISE_DB_PATH",
            os.path.join(os.path.dirname(__file__), "..", "..", "ext_document_map.db"),
        )
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        path = self._db_path
        if path == ":memory:":
            path = "file::memory:?cache=shared"
        self._db = await aiosqlite.connect(path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def ensure_table(self) -> None:
        db = await self._get_db()
        await db.executescript(CREATE_EXT_USER_MAP)
        await db.commit()

    async def insert_mapping(self, entry: ExtUserMap) -> ExtUserMap:
        now = datetime.now(timezone.utc).isoformat()
        db = await self._get_db()
        cursor = await db.execute(
            """INSERT INTO ext_user_map
               (tenant_id, business_user_id, business_subject, ragflow_user_id,
                status, mapping_strategy, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, business_subject) DO UPDATE SET
               updated_at=excluded.updated_at""",
            (entry.tenant_id, entry.business_user_id, entry.business_subject,
             entry.ragflow_user_id, entry.status, entry.mapping_strategy,
             now, now),
        )
        await db.commit()
        entry.id = cursor.lastrowid
        return entry

    async def get_mapping(
        self, tenant_id: str, business_subject: str
    ) -> dict | None:
        db = await self._get_db()
        cursor = await db.execute(
            """SELECT * FROM ext_user_map
               WHERE tenant_id=? AND business_subject=?""",
            (tenant_id, business_subject),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None

    async def record_login(self, tenant_id: str, business_subject: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        db = await self._get_db()
        await db.execute(
            """UPDATE ext_user_map SET last_login_at=?, updated_at=?
               WHERE tenant_id=? AND business_subject=?""",
            (now, now, tenant_id, business_subject),
        )
        await db.commit()
