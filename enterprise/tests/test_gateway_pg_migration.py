"""Real PostgreSQL tests for the one-time Gateway SQLite migration."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.dialect import fetchone
from enterprise.scripts.migrate_gateway_sqlite_to_postgres import migrate

TEST_URL = (
    "postgresql+asyncpg://tyrag_gateway_test:tyrag_gateway_test"
    "@127.0.0.1:55432/tyrag_gateway_test"
)


def _legacy_database(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE ext_document_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                external_document_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_name TEXT NOT NULL,
                source_size INTEGER,
                source_modified_ns INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO ext_document_map (
                tenant_id, source_system, external_document_id,
                source_version_id, event_id, sha256, file_name,
                source_size, source_modified_ns, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a",
                "EAM",
                "DOC-1",
                "v1",
                "evt-1",
                "a" * 64,
                "test.pdf",
                3_000_000_000,
                1_800_000_000_000_000_000,
                "now",
                "now",
            ),
        )


@pytest.mark.asyncio
async def test_migrate_sqlite_to_postgres_and_reject_nonempty_target(
    tmp_path, monkeypatch,
):
    source = tmp_path / "gateway.db"
    _legacy_database(source)
    schema = f"gw_migrate_{uuid.uuid4().hex[:16]}"
    monkeypatch.delenv("ENTERPRISE_DB_PATH", raising=False)
    monkeypatch.delenv("ENTERPRISE_SYNC_DB_PATH", raising=False)
    monkeypatch.setenv("ENTERPRISE_GATEWAY_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ENTERPRISE_GATEWAY_DATABASE_SCHEMA", schema)

    manifest = await migrate(source)
    assert manifest["totalRows"] == 1
    assert manifest["tables"]["ext_document_map"]["rows"] == 1
    assert "DOC-1" not in str(manifest)

    gateway = GatewayDatabase(TEST_URL, schema=schema)
    try:
        async with gateway.transaction() as conn:
            row = await fetchone(
                conn,
                "SELECT source_size, source_modified_ns "
                "FROM ext_document_map WHERE event_id=?",
                ("evt-1",),
            )
        assert row == {
            "source_size": 3_000_000_000,
            "source_modified_ns": 1_800_000_000_000_000_000,
        }
    finally:
        await gateway.dispose()

    with pytest.raises(RuntimeError, match="target is not empty"):
        await migrate(source)


@pytest.mark.asyncio
async def test_migration_dry_run_rolls_back_rows(tmp_path, monkeypatch):
    source = tmp_path / "gateway.db"
    _legacy_database(source)
    schema = f"gw_migrate_dry_{uuid.uuid4().hex[:16]}"
    monkeypatch.delenv("ENTERPRISE_DB_PATH", raising=False)
    monkeypatch.delenv("ENTERPRISE_SYNC_DB_PATH", raising=False)
    monkeypatch.setenv("ENTERPRISE_GATEWAY_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ENTERPRISE_GATEWAY_DATABASE_SCHEMA", schema)

    manifest = await migrate(source, dry_run=True)
    assert manifest["dryRun"] is True
    gateway = GatewayDatabase(TEST_URL, schema=schema)
    try:
        async with gateway.transaction() as conn:
            row = await fetchone(conn, "SELECT COUNT(*) AS n FROM ext_document_map")
        assert row == {"n": 0}
    finally:
        await gateway.dispose()
