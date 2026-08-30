"""PostgreSQL-backed SQLAlchemy Core entry point for the Enterprise Gateway."""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from enterprise.gateway.db.dialect import begin_transaction, configure_pg_engine
from enterprise.gateway.db.schema import initialize_schema

DATABASE_ENV = "ENTERPRISE_GATEWAY_DATABASE_URL"
TEST_DATABASE_ENV = "ENTERPRISE_GATEWAY_TEST_DATABASE_URL"
SCHEMA_ENV = "ENTERPRISE_GATEWAY_DATABASE_SCHEMA"
TEST_SCHEMA_ENV = "ENTERPRISE_GATEWAY_TEST_DATABASE_SCHEMA"
DEFAULT_SCHEMA = "public"


def _pg_url_from_env(*, test: bool = False) -> str:
    direct_name = TEST_DATABASE_ENV if test else DATABASE_ENV
    direct = (os.environ.get(direct_name) or "").strip()
    if direct:
        return direct

    prefix = "ENTERPRISE_GATEWAY_TEST_DB_" if test else "ENTERPRISE_GATEWAY_DB_"
    host = (os.environ.get(prefix + "HOST") or "").strip()
    name = (os.environ.get(prefix + "NAME") or "").strip()
    user = (os.environ.get(prefix + "USER") or "").strip()
    password = os.environ.get(prefix + "PASSWORD")
    port = (os.environ.get(prefix + "PORT") or "5432").strip()
    if not all((host, name, user, password)):
        raise RuntimeError(
            f"{direct_name} or {prefix}HOST/NAME/USER/PASSWORD is required"
        )
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=name,
    ).render_as_string(hide_password=False)


def resolve_database_url() -> str:
    """Resolve the production PG URL and reject the former SQLite settings."""
    legacy = [
        name for name in ("ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH")
        if (os.environ.get(name) or "").strip()
    ]
    if legacy:
        raise RuntimeError(
            "SQLite database path settings are unsupported; configure "
            f"{DATABASE_ENV}"
        )
    value = _pg_url_from_env()
    if not value.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise RuntimeError(f"{DATABASE_ENV} must use PostgreSQL")
    return value


def resolve_test_database_url() -> str:
    value = _pg_url_from_env(test=True)
    if not value.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise RuntimeError(f"{TEST_DATABASE_ENV} must use PostgreSQL")
    return value


def _safe_schema(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "gw_" + cleaned
    return cleaned[:50]


def schema_for_key(key: str | None = None, *, test: bool = False) -> str:
    configured = (os.environ.get(TEST_SCHEMA_ENV if test else SCHEMA_ENV) or "").strip()
    if configured:
        return _safe_schema(configured)
    if not test:
        return DEFAULT_SCHEMA
    digest = hashlib.sha256((key or "test").encode()).hexdigest()[:16]
    return f"gw_test_{digest}"


class GatewayDatabase:
    """Async SQLAlchemy Core database for the Enterprise Gateway."""

    def __init__(self, database_url: str, *, schema: str = DEFAULT_SCHEMA) -> None:
        if not database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("GatewayDatabase requires a PostgreSQL URL")
        self.database_url = database_url
        self.db_path = database_url  # compatibility for lifecycle comparisons
        self.schema = _safe_schema(schema) if schema != DEFAULT_SCHEMA else DEFAULT_SCHEMA
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        configure_pg_engine(self._engine, schema=self.schema)
        self._initialized = False

    @classmethod
    def from_env(cls) -> GatewayDatabase:
        return cls(resolve_database_url(), schema=schema_for_key())

    @classmethod
    def for_test(cls, key: str | None = None) -> GatewayDatabase:
        return cls(resolve_test_database_url(), schema=schema_for_key(key, test=True))

    @classmethod
    def for_path(cls, db_path: str) -> GatewayDatabase:
        """Compatibility name used by old tests/scripts; never opens a file."""
        return cls.for_test(db_path)

    @classmethod
    def in_memory(cls, cache_name: str | None = None) -> GatewayDatabase:
        """Compatibility name used by old tests; each key gets a PG schema."""
        return cls.for_test(cache_name or os.urandom(8).hex())

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def initialize(self) -> None:
        if self._initialized:
            return
        await initialize_schema(self._engine, schema=self.schema)
        self._initialized = True

    async def connect(self) -> AsyncConnection:
        return await self._engine.connect()

    @asynccontextmanager
    async def transaction(self, *, write: bool = False) -> AsyncIterator[AsyncConnection]:
        conn = await self.connect()
        try:
            await begin_transaction(conn)
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def dispose(self) -> None:
        await self._engine.dispose()
        self._initialized = False


def new_memory_cache_name() -> str:
    """Compatibility helper; returns a schema key, not a memory database."""
    return "gw_test_" + os.urandom(8).hex()
