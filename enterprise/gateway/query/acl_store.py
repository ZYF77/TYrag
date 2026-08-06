"""User-level document allowlist for the query demo router."""
from __future__ import annotations

from datetime import datetime, timezone

CREATE_DEMO_DOCUMENT_ACL = """
CREATE TABLE IF NOT EXISTS demo_document_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, external_document_id, business_user_id)
);

CREATE INDEX IF NOT EXISTS idx_demo_document_acl_user
    ON demo_document_acl(tenant_id, external_document_id);
"""


async def ensure_schema(db) -> None:
    await db.executescript(CREATE_DEMO_DOCUMENT_ACL)
    await db.commit()


async def grant(
    db,
    *,
    tenant_id: str,
    external_document_id: str,
    business_user_id: str,
) -> None:
    await ensure_schema(db)
    await db.execute(
        """INSERT INTO demo_document_acl
           (tenant_id, external_document_id, business_user_id, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(tenant_id, external_document_id, business_user_id)
           DO NOTHING""",
        (
            tenant_id,
            external_document_id,
            business_user_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()


async def is_allowed(
    db,
    *,
    tenant_id: str,
    external_document_id: str,
    business_user_id: str,
) -> bool:
    await ensure_schema(db)
    async with db.execute(
        """SELECT 1 FROM demo_document_acl
           WHERE tenant_id=?
             AND external_document_id=?
             AND business_user_id=?
           LIMIT 1""",
        (tenant_id, external_document_id, business_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None
