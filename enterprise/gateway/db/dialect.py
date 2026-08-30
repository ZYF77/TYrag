"""PostgreSQL SQL execution helpers for GatewayDatabase."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from enterprise.gateway.db.exceptions import (
    PersistenceConflictError,
    PersistenceUnavailableError,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _map_exception(exc: BaseException) -> BaseException:
    if isinstance(exc, (PersistenceConflictError, PersistenceUnavailableError)):
        return exc
    if isinstance(exc, IntegrityError):
        mapped = PersistenceConflictError(str(exc))
        mapped.__cause__ = exc
        return mapped
    if isinstance(exc, (OperationalError, DBAPIError)):
        orig = getattr(exc, "orig", None)
        sqlstate = str(
            getattr(orig, "sqlstate", None)
            or getattr(orig, "pgcode", None)
            or ""
        )
        if sqlstate.startswith("23"):
            mapped = PersistenceConflictError(str(exc))
            mapped.__cause__ = exc
            return mapped
        mapped = PersistenceUnavailableError(str(exc))
        mapped.__cause__ = exc
        return mapped
    return exc


def configure_pg_engine(engine: AsyncEngine, *, schema: str) -> None:
    """Validate the engine target without adding a second connection path."""
    if engine.url.drivername not in {"postgresql+asyncpg", "postgresql"}:
        raise ValueError("GatewayDatabase requires a PostgreSQL SQLAlchemy engine")
    if not _IDENTIFIER.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema identifier")


def _qmark_sql(sql: str, count: int) -> str:
    """Convert qmark SQL emitted by existing stores to named PostgreSQL binds."""
    out: list[str] = []
    index = 0
    i = 0
    quote: str | None = None
    while i < len(sql):
        char = sql[i]
        if quote:
            out.append(char)
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append(f":p{index}")
            index += 1
        else:
            out.append(char)
        i += 1
    if index != count:
        raise ValueError(f"SQL parameter count mismatch: expected {index}, got {count}")
    return "".join(out)


def _statement_and_params(
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] | None,
) -> tuple[Any, Mapping[str, Any] | None]:
    if parameters is None:
        return text(sql), None
    if isinstance(parameters, Mapping):
        return text(sql), dict(parameters)
    values = tuple(parameters)
    return text(_qmark_sql(sql, len(values))), {
        f"p{index}": value for index, value in enumerate(values)
    }


async def exec_sql(
    conn: AsyncConnection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] | None = None,
) -> CursorResult[Any]:
    try:
        statement, values = _statement_and_params(sql, parameters)
        if values is None:
            return await conn.execute(statement)
        return await conn.execute(statement, values)
    except Exception as exc:
        mapped = _map_exception(exc)
        if mapped is not exc:
            raise mapped from exc
        raise


async def fetchone(
    conn: AsyncConnection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    result = await exec_sql(conn, sql, parameters)
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetchall(
    conn: AsyncConnection,
    sql: str,
    parameters: Sequence[Any] | Mapping[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    result = await exec_sql(conn, sql, parameters)
    return [dict(row) for row in result.mappings().all()]


async def executescript(conn: AsyncConnection, script: str) -> None:
    """Run a semicolon-separated SQL script statement-by-statement."""
    for statement in script.split(";"):
        stmt = statement.strip()
        if stmt:
            await exec_sql(conn, stmt)


async def table_columns(conn: AsyncConnection, table: str) -> set[str]:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("invalid PostgreSQL table identifier")
    rows = await fetchall(
        conn,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=?",
        (table,),
    )
    return {str(row["column_name"]) for row in rows}


async def add_column_if_missing(
    conn: AsyncConnection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if not _IDENTIFIER.fullmatch(table) or not _IDENTIFIER.fullmatch(column):
        raise ValueError("invalid PostgreSQL identifier")
    await exec_sql(
        conn,
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}",
    )


async def begin_transaction(conn: AsyncConnection) -> None:
    if not conn.in_transaction():
        await conn.begin()

def pg_on_conflict_do_nothing(columns: Sequence[str]) -> str:
    cols = ", ".join(columns)
    return f"ON CONFLICT({cols}) DO NOTHING"


def pg_on_conflict_do_update(
    conflict_columns: Sequence[str],
    update_columns: Sequence[str],
) -> str:
    conflict = ", ".join(conflict_columns)
    updates = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    return f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"


async def claim_rows(
    conn: AsyncConnection,
    *,
    table: str,
    claim_set_sql: str,
    select_sql: str,
    parameters: Sequence[Any],
) -> list[Mapping[str, Any]]:
    """Run a caller-supplied PostgreSQL claim statement and return its rows."""
    del table
    result = await exec_sql(conn, claim_set_sql, parameters)
    if result.returns_rows:
        return [dict(row) for row in result.mappings().all()]
    return await fetchall(conn, select_sql, parameters)

