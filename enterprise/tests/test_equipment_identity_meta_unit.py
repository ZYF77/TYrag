"""No-DB unit coverage for identity RAGFlow meta projection."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from enterprise.gateway.equipment_identity import (
    CurrentEquipmentIdentity,
    preview_identifier_pattern,
    validate_identifier_pattern,
)
from enterprise.gateway.sync.models import ExtDocumentMap, OutboxEvent
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import SyncService


@pytest.mark.asyncio
async def test_process_identity_event_writes_asset_and_version_without_parse():
    ragflow = RAGFlowDocumentStub()
    dataset = await ragflow.create_dataset("unit-identity")
    dataset_id = dataset["data"]["id"]
    uploaded = await ragflow.upload_document(dataset_id, "manual.pdf", b"pdf")
    document_id = uploaded["data"][0]["id"]
    await ragflow.update_document_metadata(
        dataset_id,
        document_id,
        {
            "equipment_id": "EQ-1",
            "fixed_asset_no": "FA-OLD",
            "asset_id": "ASSET-OLD",
            "keep": 1,
        },
    )

    identity = CurrentEquipmentIdentity(
        tenant_id="tenant-a",
        equipment_id="EQ-1",
        fixed_asset_no="FA-NEW",
        asset_id="ASSET-NEW",
        source_system="EAM",
        identity_version=9,
        updated_at="now",
    )
    doc = SimpleNamespace(
        ragflow_dataset_id=dataset_id,
        ragflow_document_id=document_id,
    )

    service = SyncService(SimpleNamespace(), SourceStub(), ragflow)

    async def _db_call(fn, *args, write=False, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_current_identity":
            return identity
        if name == "list_mappings_for_equipment":
            return [doc]
        raise AssertionError(f"unexpected db call {fn} {args} {kwargs}")

    service._db_call = _db_call  # type: ignore[method-assign]
    await service.process_identity_event(
        OutboxEvent(
            event_id="equipment-identity:tenant-a:EQ-1:9",
            event_type="equipment_identity_update",
            tenant_id="tenant-a",
            source_system="EAM",
            external_document_id="EQ-1",
            source_version_id="9",
            payload=json.dumps({"identityVersion": 9}),
        )
    )
    remote = (await ragflow.list_documents(dataset_id, document_id=document_id))[0]
    assert remote["meta_fields"]["asset_id"] == "ASSET-NEW"
    assert remote["meta_fields"]["enterprise_identity_version"] == 9
    assert remote["meta_fields"]["fixed_asset_no"] == "FA-NEW"
    assert remote["meta_fields"]["keep"] == 1
    assert "parse" not in ragflow._operation_log


@pytest.mark.asyncio
async def test_process_identity_event_deletes_cleared_keys():
    ragflow = RAGFlowDocumentStub()
    dataset = await ragflow.create_dataset("unit-clear")
    dataset_id = dataset["data"]["id"]
    uploaded = await ragflow.upload_document(dataset_id, "manual.pdf", b"pdf")
    document_id = uploaded["data"][0]["id"]
    await ragflow.update_document_metadata(
        dataset_id,
        document_id,
        {
            "equipment_id": "EQ-1",
            "fixed_asset_no": "FA-OLD",
            "asset_id": "ASSET-OLD",
        },
    )
    identity = CurrentEquipmentIdentity(
        tenant_id="tenant-a",
        equipment_id="EQ-1",
        fixed_asset_no=None,
        asset_id=None,
        source_system="EAM",
        identity_version=4,
        updated_at="now",
    )
    doc = SimpleNamespace(
        ragflow_dataset_id=dataset_id,
        ragflow_document_id=document_id,
    )
    service = SyncService(SimpleNamespace(), SourceStub(), ragflow)

    async def _db_call(fn, *args, write=False, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_current_identity":
            return identity
        if name == "list_mappings_for_equipment":
            return [doc]
        raise AssertionError(name)

    service._db_call = _db_call  # type: ignore[method-assign]
    await service.process_identity_event(
        OutboxEvent(
            event_id="equipment-identity:tenant-a:EQ-1:4",
            event_type="equipment_identity_update",
            tenant_id="tenant-a",
            source_system="EAM",
            external_document_id="EQ-1",
            source_version_id="4",
            payload=json.dumps({"identityVersion": 4}),
        )
    )
    remote = (await ragflow.list_documents(dataset_id, document_id=document_id))[0]
    assert remote["meta_fields"]["equipment_id"] == "EQ-1"
    assert remote["meta_fields"]["enterprise_identity_version"] == 4
    assert "fixed_asset_no" not in remote["meta_fields"]
    assert "asset_id" not in remote["meta_fields"]


def test_recognition_pattern_rejects_lookahead_keeps_stdlib_re():
    with pytest.raises(ValueError):
        validate_identifier_pattern(r"(?=EQ)")
    assert preview_identifier_pattern("EQ-9 EQ-9", r"EQ-\d+") == ["EQ-9"]


def _sample_doc(**overrides):
    values = dict(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name="manual.pdf",
        equipment_id="EQ-1",
        fixed_asset_no="FA-1",
    )
    values.update(overrides)
    return ExtDocumentMap(**values)


def test_external_meta_fields_includes_registration_attributes():
    doc = _sample_doc()
    event = OutboxEvent(
        event_id="evt-1",
        event_type="upsert",
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="1",
        payload=json.dumps(
            {
                "metadata": {
                    "model": "MODEL-X",
                    "equipment_type": "PUMP",
                    "manufacturer": "ACME",
                    "equipment_id": "EQ-1",
                }
            }
        ),
    )
    meta = SyncService._external_meta_fields(doc, event)
    assert meta["equipment_id"] == "EQ-1"
    assert meta["fixed_asset_no"] == "FA-1"
    assert meta["model"] == "MODEL-X"
    assert meta["equipment_type"] == "PUMP"
    assert meta["manufacturer"] == "ACME"


def test_external_meta_fields_accepts_equipmentType_alias():
    doc = _sample_doc()
    event = OutboxEvent(
        event_id="evt-2",
        event_type="upsert",
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="1",
        payload=json.dumps({"metadata": {"equipmentType": "VALVE", "model": "M2"}}),
    )
    meta = SyncService._external_meta_fields(doc, event)
    assert meta["equipment_type"] == "VALVE"
    assert meta["model"] == "M2"
    assert "manufacturer" not in meta


def test_external_meta_fields_omits_missing_registration_attributes():
    doc = _sample_doc()
    event = OutboxEvent(
        event_id="evt-3",
        event_type="upsert",
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="1",
        payload=json.dumps({"metadata": {"equipment_id": "EQ-1"}}),
    )
    meta = SyncService._external_meta_fields(doc, event)
    assert "model" not in meta
    assert "equipment_type" not in meta
    assert "manufacturer" not in meta


@pytest.mark.asyncio
async def test_ensure_enterprise_metadata_merge_preserves_other_keys():
    ragflow = RAGFlowDocumentStub()
    dataset = await ragflow.create_dataset("unit-reg-meta")
    dataset_id = dataset["data"]["id"]
    uploaded = await ragflow.upload_document(dataset_id, "manual.pdf", b"pdf")
    document_id = uploaded["data"][0]["id"]
    await ragflow.update_document_metadata(
        dataset_id,
        document_id,
        {"keep": "yes", "equipment_id": "EQ-OLD"},
    )
    # Stub list returns run=UNSTART by default so ensure path updates meta.
    docs = await ragflow.list_documents(dataset_id, document_id=document_id)
    docs[0]["run"] = "UNSTART"

    doc = _sample_doc(ragflow_dataset_id=dataset_id, ragflow_document_id=document_id)
    event = OutboxEvent(
        event_id="evt-merge",
        event_type="upsert",
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="1",
        payload=json.dumps(
            {
                "metadata": {
                    "model": "MX",
                    "manufacturer": "ACME",
                    "equipment_type": "PUMP",
                }
            }
        ),
    )
    service = SyncService(SimpleNamespace(), SourceStub(), ragflow)
    updated = await service._ensure_enterprise_metadata(doc, dataset_id, event)
    remote = updated.get("meta_fields") or (
        await ragflow.list_documents(dataset_id, document_id=document_id)
    )[0]["meta_fields"]
    assert remote["keep"] == "yes"
    assert remote["model"] == "MX"
    assert remote["manufacturer"] == "ACME"
    assert remote["equipment_type"] == "PUMP"
    assert remote["equipment_id"] == "EQ-1"
