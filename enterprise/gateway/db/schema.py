"""PostgreSQL schema bootstrap for the Enterprise Gateway."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from enterprise.gateway.db.dialect import add_column_if_missing, exec_sql
from enterprise.gateway.db.tables import metadata

SCHEMA_VERSION = 2


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _upgrade_v1_to_v2(conn) -> None:
    """Add processing-round state without replacing existing Gateway data."""
    await add_column_if_missing(
        conn, "ext_document_map", "processing_round", "INTEGER NOT NULL DEFAULT 1"
    )
    await add_column_if_missing(
        conn, "sync_outbox", "processing_round", "INTEGER NOT NULL DEFAULT 1"
    )
    await add_column_if_missing(
        conn,
        "ext_document_map",
        "last_error_retryable",
        "INTEGER NOT NULL DEFAULT 0",
    )
    await add_column_if_missing(
        conn, "callback_delivery", "processing_round", "INTEGER NOT NULL DEFAULT 1"
    )
    await exec_sql(
        conn,
        """UPDATE ext_document_map
              SET processing_round=COALESCE(processing_round, 1),
                  last_error_retryable=CASE
                    WHEN sync_status='failed'
                     AND last_error_code IN (
                       'DOCUMENT_SOURCE_NOT_FOUND',
                       'DOCUMENT_SYNC_FAILED',
                       'DOCUMENT_PARSE_FAILED',
                       'RAGFLOW_UNAVAILABLE'
                     ) THEN 1 ELSE 0 END""",
    )

    result = await conn.execute(
        text(
            """SELECT c.conname,
                      array_agg(a.attname ORDER BY k.ordinality) AS columns
                 FROM pg_constraint c
                 JOIN pg_class t ON t.oid=c.conrelid
                 JOIN pg_namespace n
                   ON n.oid=t.relnamespace AND n.nspname=current_schema()
                 JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
                   ON TRUE
                 JOIN pg_attribute a
                   ON a.attrelid=t.oid AND a.attnum=k.attnum
                WHERE t.relname='callback_delivery' AND c.contype='u'
                GROUP BY c.conname"""
        )
    )
    old_columns = {
        "tenant_id",
        "source_system",
        "external_document_id",
        "source_version_id",
        "terminal_status",
    }
    new_name = "uq_callback_delivery_round_terminal"
    found_new = False
    for row in result:
        name = str(row[0])
        columns = set(row[1] or [])
        if name == new_name:
            found_new = True
        elif columns == old_columns:
            await conn.execute(
                text(
                    "ALTER TABLE callback_delivery DROP CONSTRAINT "
                    f"{_quote_identifier(name)}"
                )
            )
    if not found_new:
        await conn.execute(
            text(
                "ALTER TABLE callback_delivery ADD CONSTRAINT "
                f"{_quote_identifier(new_name)} UNIQUE ("
                "tenant_id, source_system, external_document_id, "
                "source_version_id, processing_round, terminal_status)"
            )
        )


async def initialize_schema(engine: AsyncEngine, *, schema: str = "public") -> None:
    """Create or upgrade the Gateway schema and reject unknown versions."""
    if not schema.replace("_", "").isalnum() or not schema[0].isalpha():
        raise ValueError("invalid PostgreSQL schema identifier")
    async with engine.begin() as conn:
        if schema != "public":
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        existing = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema()"
            )
        )
        existing_names = {str(row[0]) for row in existing}
        expected_names = set(metadata.tables)
        if existing_names and existing_names != expected_names:
            missing = sorted(expected_names - existing_names)
            extra = sorted(existing_names - expected_names)
            raise RuntimeError(
                f"incomplete Gateway schema: missing={missing!r}, extra={extra!r}"
            )
        if not existing_names:
            await conn.run_sync(metadata.create_all)
        # 新库由 create_all 建列；老库（含 v1/v2）在此幂等补列，不 bump 版本。
        await add_column_if_missing(
            conn, "ext_document_map", "parsed_at", "TEXT"
        )
        result = await conn.execute(
            text("SELECT version FROM gateway_schema_version ORDER BY version")
        )
        values = [int(row[0]) for row in result]
        if values == [1]:
            await _upgrade_v1_to_v2(conn)
        elif values not in ([], [SCHEMA_VERSION]):
            raise RuntimeError(
                f"unsupported Gateway schema version: {values!r}; "
                f"expected [1] or [{SCHEMA_VERSION}]"
            )
        await conn.execute(text("DELETE FROM gateway_schema_version"))
        await conn.execute(
            text(
                "INSERT INTO gateway_schema_version(version, applied_at) "
                "VALUES (:version, :applied_at)"
            ),
            {
                "version": SCHEMA_VERSION,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            },
        )
