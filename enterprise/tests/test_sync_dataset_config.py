"""Focused tests for FILE_SHARE RAGFlow dataset configuration."""

import pytest

from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
)
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import (
    SyncService,
    TerminalDocumentSyncError,
)


@pytest.mark.asyncio
async def test_dataset_defaults_preserve_tenant_name_and_creation_payload(monkeypatch):
    monkeypatch.delenv("ENTERPRISE_RAGFLOW_DATASET_NAME", raising=False)
    monkeypatch.delenv("ENTERPRISE_RAGFLOW_DATASET_PERMISSION", raising=False)
    client = RAGFlowDocumentStub()
    service = SyncService(None, SourceStub(), client)

    dataset = await service._ensure_dataset("tenant-001")

    assert dataset["data"] == {
        "id": dataset["data"]["id"],
        "name": "enterprise-tenant-001",
    }


@pytest.mark.asyncio
async def test_dataset_name_and_team_permission_can_be_overridden(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_DATASET_NAME", "设备问询知识库")
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_DATASET_PERMISSION", "team")
    client = RAGFlowDocumentStub()
    service = SyncService(None, SourceStub(), client)

    dataset = await service._ensure_dataset("tenant-001")

    assert dataset["data"]["name"] == "设备问询知识库"
    assert dataset["data"]["permission"] == "team"


@pytest.mark.asyncio
async def test_invalid_dataset_permission_fails_before_ragflow_call(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_DATASET_PERMISSION", "public")
    client = RAGFlowDocumentStub()
    service = SyncService(None, SourceStub(), client)

    with pytest.raises(TerminalDocumentSyncError) as exc_info:
        await service._ensure_dataset("tenant-001")

    assert exc_info.value.code == "RAGFLOW_DATASET_PERMISSION_INVALID"
    assert await client.list_datasets() == []


@pytest.mark.asyncio
async def test_real_client_only_adds_permission_when_configured(monkeypatch):
    client = RAGFlowDocumentClient(base_url="http://ragflow.test", api_key="test-key")
    payloads = []

    async def fake_run_sync(_fn, _method, _path, _request_id, **kwargs):
        payloads.append(kwargs["json_data"])
        return {"code": 0, "data": {"id": "dataset-id"}}

    monkeypatch.setattr(client, "_run_sync", fake_run_sync)

    await client.create_dataset("default")
    await client.create_dataset("shared", permission="team")

    assert payloads == [
        {"name": "default", "description": ""},
        {"name": "shared", "description": "", "permission": "team"},
    ]
