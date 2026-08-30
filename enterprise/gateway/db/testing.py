"""Test helpers for GatewayDatabase fixtures."""

from __future__ import annotations

import uuid

from enterprise.gateway.db.database import (
    GatewayDatabase,
    schema_for_key,
)


async def create_gateway(
    db_path: str | None = ":memory:",
    *,
    schema: str | None = None,
) -> GatewayDatabase:
    if db_path is None:
        gateway = GatewayDatabase.from_env()
    elif db_path == ":memory:":
        gateway = GatewayDatabase.for_test(f"memory-{uuid.uuid4().hex}")
    elif db_path.startswith(("postgresql+asyncpg://", "postgresql://")):
        gateway = GatewayDatabase(
            db_path,
            schema=schema or schema_for_key(db_path, test=True),
        )
    else:
        gateway = GatewayDatabase.for_test(db_path)
    await gateway.initialize()
    return gateway
