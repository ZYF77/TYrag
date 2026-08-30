"""User-level document allowlist for the query demo router."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import exec_sql, fetchone


async def grant(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    external_document_id: str,
    business_user_id: str,
) -> None:
    await exec_sql(
        conn,
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


async def is_allowed(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    external_document_id: str,
    business_user_id: str,
) -> bool:
    row = await fetchone(
        conn,
        """SELECT 1 FROM demo_document_acl
           WHERE tenant_id=?
             AND external_document_id=?
             AND business_user_id=?
           LIMIT 1""",
        (tenant_id, external_document_id, business_user_id),
    )
    return row is not None
