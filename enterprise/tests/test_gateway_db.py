"""GatewayDatabase infrastructure tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from enterprise.gateway.callback_delivery import claim_pending_callback_deliveries
from enterprise.gateway.db import GatewayDatabase
from enterprise.gateway.db.database import resolve_database_url
from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.schema import initialize_schema
from enterprise.gateway.db.testing import create_gateway
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    OutboxEvent,
    claim_outbox,
    enqueue_outbox,
    get_outbox_by_event_id,
    insert_mapping,
    mark_outbox_done,
)


@pytest.mark.asyncio
async def test_gateway_transaction_commit_and_rollback():
    gateway = GatewayDatabase.in_memory()
    await gateway.initialize()
    try:
        async with gateway.transaction(write=True) as conn:
            await exec_sql(
                conn,
                "INSERT INTO ext_user_map "
                "(tenant_id, business_subject, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("t1", "user-a", "now", "now"),
            )
        async with gateway.transaction() as conn:
            row = await fetchone(
                conn,
                "SELECT business_subject FROM ext_user_map WHERE tenant_id=?",
                ("t1",),
            )
            assert row is not None
            assert row["business_subject"] == "user-a"
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_gateway_schema_upgrade_is_idempotent_for_v1_marker():
    gateway = GatewayDatabase.in_memory()
    await gateway.initialize()
    try:
        async with gateway.transaction(write=True) as conn:
            await exec_sql(conn, "DELETE FROM gateway_schema_version")
            await exec_sql(
                conn,
                "INSERT INTO gateway_schema_version(version, applied_at) VALUES (?, ?)",
                (1, "legacy"),
            )
        await initialize_schema(gateway.engine, schema=gateway.schema)
        async with gateway.transaction() as conn:
            version = await fetchone(
                conn,
                "SELECT version FROM gateway_schema_version",
            )
            columns = await fetchall(
                conn,
                """SELECT table_name, column_name
                     FROM information_schema.columns
                    WHERE table_schema=current_schema()
                      AND table_name IN ('ext_document_map', 'sync_outbox', 'callback_delivery')
                      AND column_name='processing_round'""",
            )
        assert version == {"version": 3}
        assert {(row["table_name"], row["column_name"]) for row in columns} == {
            ("ext_document_map", "processing_round"),
            ("sync_outbox", "processing_round"),
            ("callback_delivery", "processing_round"),
        }
    finally:
        await gateway.dispose()


def test_resolve_database_rejects_legacy_sqlite(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_DB_PATH", "gateway.db")
    with pytest.raises(RuntimeError, match="SQLite database path settings"):
        resolve_database_url()


def test_gateway_database_rejects_non_postgres_url():
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        GatewayDatabase("sqlite+aiosqlite:///gateway.db")


@pytest.mark.asyncio
async def test_insert_mapping_roundtrip(db):
    doc = ExtDocumentMap(
        tenant_id="t1",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="v1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name="test.pdf",
    )
    inserted = await insert_mapping(db, doc)
    assert inserted.id is not None


@pytest.mark.asyncio
async def test_postgres_claims_are_disjoint_across_workers():
    gateway = await create_gateway(":memory:")
    try:
        async with gateway.transaction(write=True) as conn:
            for index in range(4):
                await enqueue_outbox(
                    conn,
                    OutboxEvent(
                        event_id=f"evt-{index}",
                        event_type="upsert",
                        tenant_id="t1",
                        source_system="EAM",
                        external_document_id=f"DOC-{index}",
                        source_version_id="v1",
                        payload=json.dumps({"index": index}),
                    ),
                )
                await exec_sql(
                    conn,
                    """INSERT INTO callback_delivery (
                        delivery_id, originating_event_id, tenant_id, source_system,
                        external_document_id, source_version_id, terminal_status,
                        payload_json, payload_hash, endpoint_url, attempts,
                        max_attempts, state, created_at, updated_at
                    ) VALUES (?, ?, 't1', 'EAM', ?, 'v1', 'retrievable',
                              '{}', ?, 'https://example.invalid/callback', 0,
                              8, 'pending', 'now', 'now')""",
                    (f"delivery-{index}", f"evt-{index}", f"DOC-{index}", "a" * 64),
                )

        async def claim(worker: str):
            async with gateway.transaction(write=True) as conn:
                outbox = await claim_outbox(conn, worker, limit=2)
                callbacks = await claim_pending_callback_deliveries(
                    conn, worker_id=worker, limit=2,
                )
                return outbox, callbacks

        claimed = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        outbox = [item for group, _ in claimed for item in group]
        callbacks = [item for _, group in claimed for item in group]
        assert {item.event_id for item in outbox} == {
            "evt-0", "evt-1", "evt-2", "evt-3",
        }
        assert {item.delivery_id for item in callbacks} == {
            "delivery-0", "delivery-1", "delivery-2", "delivery-3",
        }
        assert all(item.attempts == 1 for item in outbox)
        assert all(item.attempts == 1 and item.state == "processing" for item in callbacks)
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_stale_outbox_worker_cannot_complete_requeued_round():
    gateway = await create_gateway(":memory:")
    try:
        event = OutboxEvent(
            event_id="evt-stale-round",
            event_type="upsert",
            tenant_id="t1",
            source_system="EAM",
            external_document_id="DOC-stale-round",
            source_version_id="v1",
            payload=json.dumps({"ok": True}),
        )
        async with gateway.transaction(write=True) as conn:
            await enqueue_outbox(conn, event)
            claimed = (await claim_outbox(conn, "worker-old"))[0]
            await exec_sql(
                conn,
                """UPDATE sync_outbox
                      SET processing_round=2, status='pending',
                          locked_at=NULL, worker_id=NULL
                    WHERE id=?""",
                (claimed.id,),
            )
            await mark_outbox_done(conn, claimed)

        async with gateway.transaction(write=False) as conn:
            current = await get_outbox_by_event_id(conn, event.event_id)
        assert current is not None
        assert current.processing_round == 2
        assert current.status == "pending"
    finally:
        await gateway.dispose()
