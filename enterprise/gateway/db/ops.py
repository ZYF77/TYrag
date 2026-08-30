"""Shared GatewayDatabase read/write helpers for routers and adapters."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from enterprise.gateway.db.database import GatewayDatabase

T = TypeVar("T")


async def gw_read(db: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    if isinstance(db, GatewayDatabase):
        async with db.transaction(write=False) as conn:
            return await fn(conn, *args, **kwargs)
    return await fn(db, *args, **kwargs)


async def gw_write(db: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    if isinstance(db, GatewayDatabase):
        async with db.transaction(write=True) as conn:
            return await fn(conn, *args, **kwargs)
    return await fn(db, *args, **kwargs)
