"""ExtUserMap persistence via GatewayDatabase."""

from __future__ import annotations

import asyncio

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db import GatewayDatabase
from enterprise.gateway.db.dialect import exec_sql, fetchone

logger = logging.getLogger(__name__)


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
    """Repository for ext_user_map backed by GatewayDatabase."""

    def __init__(
        self,
        gateway: GatewayDatabase | None = None,
        *,
        db_path: str | None = None,
    ):
        if gateway is not None and db_path is not None:
            raise TypeError("pass gateway or db_path, not both")
        self._gateway = gateway
        self._db_path = db_path
        self._init_lock = asyncio.Lock()

    async def _gateway_db(self) -> GatewayDatabase:
        if self._gateway is not None:
            return self._gateway
        async with self._init_lock:
            if self._gateway is not None:
                return self._gateway
            if self._db_path is not None:
                from enterprise.gateway.db.testing import create_gateway

                self._gateway = await create_gateway(self._db_path)
                return self._gateway
            from enterprise.gateway.app import get_gateway_db

            self._gateway = await get_gateway_db()
            return self._gateway

    async def insert_mapping(self, entry: ExtUserMap) -> ExtUserMap:
        now = datetime.now(timezone.utc).isoformat()
        gateway = await self._gateway_db()
        async with gateway.transaction(write=True) as conn:
            result = await exec_sql(
                conn,
                """INSERT INTO ext_user_map
                   (tenant_id, business_user_id, business_subject, ragflow_user_id,
                    status, mapping_strategy, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, business_subject) DO UPDATE SET
                   updated_at=excluded.updated_at
                   RETURNING id""",
                (
                    entry.tenant_id,
                    entry.business_user_id,
                    entry.business_subject,
                    entry.ragflow_user_id,
                    entry.status,
                    entry.mapping_strategy,
                    now,
                    now,
                ),
            )
            entry.id = int(result.scalar_one())
            return entry

    async def get_mapping(
        self, tenant_id: str, business_subject: str
    ) -> dict | None:
        gateway = await self._gateway_db()
        async with gateway.transaction() as conn:
            row = await fetchone(
                conn,
                """SELECT * FROM ext_user_map
                   WHERE tenant_id=? AND business_subject=?""",
                (tenant_id, business_subject),
            )
            return dict(row) if row else None

    async def record_login(self, tenant_id: str, business_subject: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        gateway = await self._gateway_db()
        async with gateway.transaction(write=True) as conn:
            await exec_sql(
                conn,
                """UPDATE ext_user_map SET last_login_at=?, updated_at=?
                   WHERE tenant_id=? AND business_subject=?""",
                (now, now, tenant_id, business_subject),
            )

    async def close(self) -> None:
        """Compatibility no-op; GatewayDatabase lifecycle is app-managed."""

    async def ensure_table(self) -> None:
        """Ensure GatewayDatabase is initialized (compat with legacy callers)."""
        await self._gateway_db()
