"""Equipment identity update and RAGFlow metadata projection tests."""
from __future__ import annotations

import json

import pytest

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.equipment_identity import (
    get_recognition_settings,
    preview_identifier_pattern,
    resolve_identity,
    retry_identity_sync,
    save_recognition_settings,
    upsert_current_identity,
)
from enterprise.gateway.sync.models import OutboxEvent, enqueue_outbox
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import SyncService


@pytest.mark.asyncio
async def test_identity_update_is_versioned_and_updates_gateway_document_projection(
    gateway_db,
):
    async with gateway_db.transaction(write=True) as conn:
        identity, outcome, changed = await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-OLD",
            asset_id="ASSET-OLD",
            identity_version=1,
        )
        assert (outcome, changed) == ("updated", True)
        await exec_sql(
            conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, sha256, file_name, equipment_id, fixed_asset_no,
                asset_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a", "EAM", "DOC-1", "v1", "evt-1", "a" * 64,
                "manual.pdf", "EQ-1001", "FA-OLD", "ASSET-OLD", "now", "now",
            ),
        )
        current, outcome, changed = await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-NEW",
            asset_id="ASSET-NEW",
            identity_version=2,
        )
        assert (current.fixed_asset_no, current.asset_id) == ("FA-NEW", "ASSET-NEW")
        assert (outcome, changed) == ("updated", True)
        stale, outcome, changed = await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-OLD",
            asset_id="ASSET-OLD",
            identity_version=1,
        )
        assert (stale.fixed_asset_no, stale.identity_version) == ("FA-NEW", 2)
        assert (outcome, changed) == ("stale", False)
        row = await fetchone(
            conn,
            """SELECT fixed_asset_no, asset_id
                 FROM ext_document_map
                WHERE tenant_id=? AND equipment_id=?""",
            ("tenant-a", "EQ-1001"),
        )
        assert row == {"fixed_asset_no": "FA-NEW", "asset_id": "ASSET-NEW"}


@pytest.mark.asyncio
async def test_identity_event_patches_ragflow_metadata_without_parse(gateway_db):
    ragflow = RAGFlowDocumentStub()
    dataset = await ragflow.create_dataset("identity-test")
    dataset_id = dataset["data"]["id"]
    uploaded = await ragflow.upload_document(dataset_id, "manual.pdf", b"pdf")
    document_id = uploaded["data"][0]["id"]
    # Seed stale aliases that must be replaced/cleared consistently.
    await ragflow.update_document_metadata(
        dataset_id,
        document_id,
        {
            "equipment_id": "EQ-1001",
            "fixed_asset_no": "FA-OLD",
            "asset_id": "ASSET-OLD",
            "unrelated": "keep-me",
        },
    )

    async with gateway_db.transaction(write=True) as conn:
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-NEW",
            asset_id="ASSET-NEW",
            identity_version=2,
        )
        await exec_sql(
            conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, sha256, file_name, equipment_id, fixed_asset_no,
                ragflow_dataset_id, ragflow_document_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a", "EAM", "DOC-1", "v1", "evt-1", "b" * 64,
                "manual.pdf", "EQ-1001", "FA-OLD", dataset_id, document_id,
                "now", "now",
            ),
        )

    service = SyncService(gateway_db, SourceStub(), ragflow)
    await service.process_identity_event(
        OutboxEvent(
            event_id="equipment-identity:tenant-a:EQ-1001:2",
            event_type="equipment_identity_update",
            tenant_id="tenant-a",
            source_system="EAM",
            external_document_id="EQ-1001",
            source_version_id="2",
            payload=json.dumps({
                "tenantId": "tenant-a",
                "equipmentId": "EQ-1001",
                "fixedAssetNo": "FA-NEW",
                "assetId": "ASSET-NEW",
                "identityVersion": 2,
            }),
        )
    )
    remote = (await ragflow.list_documents(dataset_id, document_id=document_id))[0]
    assert remote["meta_fields"]["equipment_id"] == "EQ-1001"
    assert remote["meta_fields"]["fixed_asset_no"] == "FA-NEW"
    assert remote["meta_fields"]["asset_id"] == "ASSET-NEW"
    assert remote["meta_fields"]["enterprise_identity_version"] == 2
    assert remote["meta_fields"]["unrelated"] == "keep-me"
    assert "parse" not in ragflow._operation_log


@pytest.mark.asyncio
async def test_identity_event_omits_cleared_variable_identifiers(gateway_db):
    ragflow = RAGFlowDocumentStub()
    dataset = await ragflow.create_dataset("identity-clear")
    dataset_id = dataset["data"]["id"]
    uploaded = await ragflow.upload_document(dataset_id, "manual.pdf", b"pdf")
    document_id = uploaded["data"][0]["id"]
    await ragflow.update_document_metadata(
        dataset_id,
        document_id,
        {
            "equipment_id": "EQ-1001",
            "fixed_asset_no": "FA-OLD",
            "asset_id": "ASSET-OLD",
            "enterprise_identity_version": 1,
        },
    )

    async with gateway_db.transaction(write=True) as conn:
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no=None,
            asset_id=None,
            identity_version=3,
        )
        await exec_sql(
            conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, sha256, file_name, equipment_id, fixed_asset_no,
                ragflow_dataset_id, ragflow_document_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a", "EAM", "DOC-2", "v1", "evt-2", "c" * 64,
                "manual.pdf", "EQ-1001", None, dataset_id, document_id,
                "now", "now",
            ),
        )

    service = SyncService(gateway_db, SourceStub(), ragflow)
    await service.process_identity_event(
        OutboxEvent(
            event_id="equipment-identity:tenant-a:EQ-1001:3",
            event_type="equipment_identity_update",
            tenant_id="tenant-a",
            source_system="EAM",
            external_document_id="EQ-1001",
            source_version_id="3",
            payload=json.dumps({
                "tenantId": "tenant-a",
                "equipmentId": "EQ-1001",
                "fixedAssetNo": None,
                "assetId": None,
                "identityVersion": 3,
            }),
        )
    )
    remote = (await ragflow.list_documents(dataset_id, document_id=document_id))[0]
    assert remote["meta_fields"]["equipment_id"] == "EQ-1001"
    assert remote["meta_fields"]["enterprise_identity_version"] == 3
    assert "fixed_asset_no" not in remote["meta_fields"]
    assert "asset_id" not in remote["meta_fields"]
    assert "parse" not in ragflow._operation_log


@pytest.mark.asyncio
async def test_recognition_defaults_enabled_and_crud_preview(gateway_db):
    async with gateway_db.transaction(write=True) as conn:
        defaults = await get_recognition_settings(conn, "tenant-a")
        assert defaults["enabled"] is True
        assert defaults["configVersion"] == 0

        saved = await save_recognition_settings(
            conn,
            tenant_id="tenant-a",
            pattern=r"[A-Za-z0-9][A-Za-z0-9._\-]{3,127}",
            enabled=True,
            expected_version=0,
            updated_by="admin@example.com",
        )
        assert saved["enabled"] is True
        assert saved["configVersion"] == 1

        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-1001",
            asset_id="ASSET-1001",
            identity_version=1,
        )

    candidates = preview_identifier_pattern(
        "请查 EQ-1001 和 EQ-1001 资料",
        saved["pattern"],
    )
    assert candidates == ["EQ-1001"]


@pytest.mark.asyncio
async def test_admin_identity_sync_retry_is_idempotent(gateway_db):
    async with gateway_db.transaction(write=True) as conn:
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-1",
            asset_id="ASSET-1",
            identity_version=2,
        )
        event = OutboxEvent(
            event_id="equipment-identity:tenant-a:EQ-1001:2",
            event_type="equipment_identity_update",
            tenant_id="tenant-a",
            source_system="EAM",
            external_document_id="EQ-1001",
            source_version_id="2",
            payload=json.dumps({
                "tenantId": "tenant-a",
                "equipmentId": "EQ-1001",
                "fixedAssetNo": "FA-1",
                "assetId": "ASSET-1",
                "identityVersion": 2,
            }),
        )
        queued = await enqueue_outbox(conn, event)
        await exec_sql(
            conn,
            """UPDATE sync_outbox
                  SET status='failed', attempts=5,
                      last_error_code=?, last_error_message=?,
                      locked_at=NULL, worker_id=NULL
                WHERE event_id=?""",
            ("RAGFLOW_UNAVAILABLE", "down", queued.event_id),
        )

        first = await retry_identity_sync(conn, "tenant-a", "EQ-1001")
        assert first["retried"] is True
        assert first["sync"]["status"] == "pending"

        second = await retry_identity_sync(conn, "tenant-a", "EQ-1001")
        assert second["retried"] is False
        assert second["sync"]["status"] == "pending"



def test_preview_pattern_rejects_empty_and_returns_unique_candidates():
    assert preview_identifier_pattern(
        "EQ-1001 和 EQ-1001",
        r"[A-Za-z0-9][A-Za-z0-9._-]{3,127}",
    ) == ["EQ-1001"]
    with pytest.raises(ValueError):
        preview_identifier_pattern("anything", r".*")

@pytest.mark.asyncio
async def test_identifier_history_tracks_reuse_and_ambiguity(gateway_db):
    async with gateway_db.transaction(write=True) as conn:
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-001",
            fixed_asset_no="FA-B",
            asset_id="ASSET-1",
            identity_version=1,
        )
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-001",
            fixed_asset_no="FA-C",
            asset_id="ASSET-1",
            identity_version=2,
        )
        rows = await fetchall(
            conn,
            """SELECT id_type, id_value, status, equipment_id
                 FROM ext_identifier_history
                WHERE tenant_id=? AND id_value IN ('FA-B', 'FA-C')
                ORDER BY id_value, status""",
            ("tenant-a",),
        )
        by_key = {(r["id_value"], r["status"]): r["equipment_id"] for r in rows}
        assert by_key[("FA-B", "historical")] == "EQ-001"
        assert by_key[("FA-C", "current")] == "EQ-001"

        # Historical FA-B uniquely maps back to EQ-001
        resolved = await resolve_identity(conn, "tenant-a", "FA-B")
        assert resolved.ambiguous is False
        assert resolved.identity is not None
        assert resolved.identity.equipment_id == "EQ-001"
        assert resolved.identity.fixed_asset_no == "FA-C"

        # Reuse FA-B onto EQ-002
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-002",
            fixed_asset_no="FA-B",
            asset_id="ASSET-2",
            identity_version=1,
        )
        current = await resolve_identity(conn, "tenant-a", "FA-B")
        assert current.ambiguous is False
        assert current.identity.equipment_id == "EQ-002"

        # Historical multi-equipment collision (no current holders) is ambiguous.
        now = "2099-01-01T00:00:00+00:00"
        for equipment_id in ("EQ-001", "EQ-002"):
            await exec_sql(
                conn,
                """INSERT INTO ext_identifier_history
                   (tenant_id, equipment_id, id_type, id_value, status,
                    valid_from, valid_to, identity_version, created_at, updated_at)
                   VALUES (?, ?, 'fixed_asset_no', 'FA-SHARED', 'historical',
                           ?, ?, 1, ?, ?)""",
                ("tenant-a", equipment_id, now, now, now, now),
            )
        amb = await resolve_identity(conn, "tenant-a", "FA-SHARED")
        assert amb.ambiguous is True
        assert set(amb.matched_equipment_ids) == {"EQ-001", "EQ-002"}


@pytest.mark.asyncio
async def test_resolve_identity_as_of_historical(gateway_db):
    async with gateway_db.transaction(write=True) as conn:
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-ASOF",
            fixed_asset_no="FA-OLD",
            asset_id=None,
            identity_version=1,
        )
        row = await fetchone(
            conn,
            """SELECT valid_from FROM ext_identifier_history
                WHERE tenant_id=? AND equipment_id=? AND id_value='FA-OLD'
                  AND status='current'""",
            ("tenant-a", "EQ-ASOF"),
        )
        valid_from = row["valid_from"]
        await upsert_current_identity(
            conn,
            tenant_id="tenant-a",
            source_system="EAM",
            equipment_id="EQ-ASOF",
            fixed_asset_no="FA-NEW",
            asset_id=None,
            identity_version=2,
        )
        asof = await resolve_identity(conn, "tenant-a", "FA-OLD", as_of=valid_from)
        assert asof.identity is not None
        assert asof.identity.equipment_id == "EQ-ASOF"

