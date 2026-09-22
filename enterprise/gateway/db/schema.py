"""PostgreSQL schema bootstrap for the Enterprise Gateway."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from enterprise.gateway.db.dialect import add_column_if_missing, exec_sql
from enterprise.gateway.db.tables import metadata

SCHEMA_VERSION = 11


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



async def _upgrade_v3_to_v4(conn) -> None:
    """Add RAGFlow document-run terminal webhook inbox."""
    await exec_sql(
        conn,
        """CREATE TABLE IF NOT EXISTS ragflow_status_inbox (
               id SERIAL PRIMARY KEY,
               event_id TEXT NOT NULL UNIQUE,
               event_type TEXT NOT NULL,
               ragflow_document_id TEXT NOT NULL,
               ragflow_dataset_id TEXT NOT NULL DEFAULT '',
               run TEXT NOT NULL,
               run_code TEXT NOT NULL DEFAULT '',
               trigger TEXT NOT NULL DEFAULT '',
               occurred_at TEXT NOT NULL DEFAULT '',
               payload_json TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL
           )""",
    )
    await exec_sql(
        conn,
        """CREATE INDEX IF NOT EXISTS idx_ragflow_status_inbox_doc
              ON ragflow_status_inbox (ragflow_document_id, run_code)""",
    )


async def _upgrade_v2_to_v3(conn) -> None:
    """Add the singleton runtime-settings row store."""
    await exec_sql(
        conn,
        """CREATE TABLE IF NOT EXISTS gateway_runtime_settings (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               settings_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               updated_by TEXT
           )""",
    )



async def _upgrade_v4_to_v5(conn) -> None:
    """Persist conversation device union + create-time anchor."""
    await add_column_if_missing(
        conn,
        "ext_v2_conversation",
        "conversation_devices",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    await add_column_if_missing(
        conn,
        "ext_v2_conversation",
        "anchor_equipment_id",
        "TEXT",
    )
    # Backfill scoped conversations created before device-union storage.
    await exec_sql(
        conn,
        """UPDATE ext_v2_conversation
              SET conversation_devices = CASE
                    WHEN equipment_id IS NOT NULL AND TRIM(equipment_id) <> ''
                      AND (conversation_devices IS NULL
                           OR TRIM(conversation_devices) IN ('', '[]'))
                    THEN json_build_array(equipment_id)::text
                    ELSE COALESCE(NULLIF(TRIM(conversation_devices), ''), '[]')
                  END,
                  anchor_equipment_id = CASE
                    WHEN anchor_equipment_id IS NULL
                     AND equipment_id IS NOT NULL
                     AND TRIM(equipment_id) <> ''
                    THEN equipment_id
                    ELSE anchor_equipment_id
                  END""",
    )


async def _upgrade_v5_to_v6(conn) -> None:
    """Persist the current EAM-owned identity snapshot on the asset registry."""
    await add_column_if_missing(
        conn, "ext_asset_registry", "source_system", "TEXT NOT NULL DEFAULT ''"
    )
    await add_column_if_missing(
        conn, "ext_asset_registry", "identity_version", "INTEGER NOT NULL DEFAULT 0"
    )
    await add_column_if_missing(
        conn, "ext_asset_registry", "updated_at", "TEXT NOT NULL DEFAULT ''"
    )
    await exec_sql(
        conn,
        """CREATE INDEX IF NOT EXISTS idx_asset_registry_fixed
              ON ext_asset_registry (tenant_id, fixed_asset_no)""",
    )
    await exec_sql(
        conn,
        """CREATE INDEX IF NOT EXISTS idx_asset_registry_asset
              ON ext_asset_registry (tenant_id, asset_id)""",
    )


async def _upgrade_v6_to_v7(conn) -> None:
    """Persist soft business context and the immutable per-run retrieval snapshot."""
    await add_column_if_missing(
        conn, "ext_v2_conversation", "business_context_json", "TEXT"
    )
    await add_column_if_missing(
        conn, "ext_v2_message_run", "retrieval_context_json", "TEXT"
    )


async def _upgrade_v7_to_v8(conn) -> None:
    """Persist the independently bound Agent Workflow test session."""
    await add_column_if_missing(conn, "ext_v2_conversation", "workflow_agent_id", "TEXT")
    await add_column_if_missing(conn, "ext_v2_conversation", "workflow_version", "TEXT")
    await add_column_if_missing(conn, "ext_v2_conversation", "workflow_session_id", "TEXT")


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
        if existing_names:
            missing = sorted(expected_names - existing_names)
            extra = sorted(existing_names - expected_names)
            allowed_missing = {
                "ext_user_preference", "ext_preference_candidate", "ext_preference_outbox",
                "gateway_runtime_settings",
                "ragflow_status_inbox",
                "gateway_equipment_recognition_settings",
            }
            if extra or (missing and not set(missing).issubset(allowed_missing)):
                raise RuntimeError(
                    f"incomplete Gateway schema: missing={missing!r}, extra={extra!r}"
                )
        if not existing_names:
            await conn.run_sync(metadata.create_all)
        elif expected_names - existing_names:
            await conn.run_sync(metadata.create_all)
        # 新库由 create_all 建列；老库（含 v1/v2）在此幂等补列并升级版本。
        await add_column_if_missing(
            conn, "ext_document_map", "parsed_at", "TEXT"
        )
        await add_column_if_missing(
            conn,
            "ext_v2_conversation",
            "conversation_devices",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        await add_column_if_missing(
            conn,
            "ext_v2_conversation",
            "anchor_equipment_id",
            "TEXT",
        )
        await add_column_if_missing(
            conn,
            "ext_v2_conversation",
            "business_context_json",
            "TEXT",
        )
        await add_column_if_missing(
            conn,
            "ext_v2_message_run",
            "retrieval_context_json",
            "TEXT",
        )
        await add_column_if_missing(conn, "ext_v2_conversation", "restart_required", "INTEGER NOT NULL DEFAULT 0")
        await add_column_if_missing(conn, "ext_v2_message", "reasoning_format", "TEXT")
        duplicates = await conn.execute(text("""SELECT COUNT(*) FROM (
            SELECT 1 FROM ext_v2_message_run WHERE status='running'
            GROUP BY tenant_id, business_user_id, conversation_id HAVING COUNT(*) > 1
        ) AS conflicts"""))
        if duplicates.scalar_one():
            raise RuntimeError("F05 migration blocked: duplicate active conversations; drain workers and reconcile runs")
        await conn.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_v2_active_conversation
            ON ext_v2_message_run(tenant_id, business_user_id, conversation_id)
            WHERE status='running'"""))
        result = await conn.execute(
            text("SELECT version FROM gateway_schema_version ORDER BY version")
        )
        values = [int(row[0]) for row in result]
        if values == [1]:
            await _upgrade_v1_to_v2(conn)
            values = [2]
        if values == [2]:
            await _upgrade_v2_to_v3(conn)
            values = [3]
        if values == [3]:
            await _upgrade_v3_to_v4(conn)
            values = [4]
        if values == [4]:
            await _upgrade_v4_to_v5(conn)
            values = [5]
        if values == [5]:
            await _upgrade_v5_to_v6(conn)
            values = [6]
        if values == [6]:
            await _upgrade_v6_to_v7(conn)
            values = [7]
        if values == [7]:
            await _upgrade_v7_to_v8(conn)
            values = [8]
        if values == [8]:
            values = [9]
        if values == [9]:
            values = [10]
        if values == [10]:
            values = [11]
        elif values not in ([], [SCHEMA_VERSION]):
            raise RuntimeError(
                f"unsupported Gateway schema version: {values!r}; "
                f"expected [1], [2], [3], [4], [5], [6], [7], or [{SCHEMA_VERSION}]"
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
