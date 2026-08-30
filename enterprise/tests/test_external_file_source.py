from __future__ import annotations

from enterprise.gateway.db.dialect import fetchone
from enterprise.gateway.db.ops import gw_read, gw_write

import hashlib
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
import yaml

from enterprise.gateway.query.source_access import RangeNotSatisfiable, parse_single_range, source_response
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.sync.external_source import FileShareConfigurationError, FileShareSourceAdapter
from enterprise.gateway.sync.models import ExtDocumentMap, get_mapping
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
)
from enterprise.gateway.sync.source_adapter import SourceAdapter, SourceFetchError
from enterprise.gateway.sync.sync_service import SyncService
from enterprise.gateway.sync.worker import OutboxWorker


REPO_ROOT = Path(__file__).resolve().parents[2]


class NeverReadSource(SourceAdapter):
    async def fetch(self, bucket, object_key, expected_sha256=None):
        raise AssertionError("FILE_SHARE ingestion must not read through the S3 adapter")


def _metadata(document_id: str, *, document_type: str = "PRODUCT_MANUAL") -> dict:
    return {
        "schema_version": 1,
        "tenant_id": "tenant-a",
        "external_document_id": document_id,
        "source_system": "DEMO",
        "equipment_id": "EQ-001",
        "document_type": document_type,
        "document_version": "v1",
        "department_id": "maintenance",
        "security_level": 2,
        "business_status": "active",
        "allow_group_ids": [],
        "deny_group_ids": [],
        "page_count": 1,
    }


def _payload(path: Path, *, event_id: str = "evt-file-share", document_id: str = "DOC-FS") -> dict:
    content = path.read_bytes()
    return {
        "eventId": event_id,
        "eventType": "upsert",
        "tenantId": "tenant-a",
        "sourceSystem": "DEMO",
        "externalDocumentId": document_id,
        "sourceVersionId": "v1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": path.name,
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "test-root",
            "relativePath": path.name,
            "size": len(content),
        },
        "metadata": _metadata(document_id),
    }


def test_file_share_opens_verified_handle_at_start_without_ticket_table(
    tmp_path,
):
    source_path = tmp_path / "设备调试记录.pdf"
    source_path.write_bytes(b"authoritative pdf bytes")
    provider = FileShareSourceAdapter({"test-root": tmp_path})
    handle = provider.open_verified(
        "test-root",
        source_path.name,
        hashlib.sha256(source_path.read_bytes()).hexdigest(),
        expected_size=source_path.stat().st_size,
    )
    try:
        assert handle.tell() == 0
        assert handle.read() == source_path.read_bytes()
    finally:
        handle.close()
    with pytest.raises(FileShareConfigurationError):
        provider.resolve_path("test-root", "../manual.pdf")
    with pytest.raises(SourceFetchError):
        provider.open_verified("test-root", "missing.pdf", "0" * 64)


def test_file_share_rejects_resolved_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    apparent_source = root / "manual.pdf"
    apparent_source.write_bytes(b"inside")
    apparent_resolved = apparent_source.resolve()
    outside_resolved = outside.resolve()
    real_resolve = Path.resolve

    def resolve(path, strict=False):
        resolved = real_resolve(path, strict=strict)
        return outside_resolved if resolved == apparent_resolved else resolved

    monkeypatch.setattr(Path, "resolve", resolve)
    provider = FileShareSourceAdapter({"test-root": root})
    with pytest.raises(FileShareConfigurationError):
        provider.resolve_path("test-root", apparent_source.name)


@pytest.mark.asyncio
async def test_v3_file_share_worker_uploads_same_verified_handle(
    isolated_gateway_db, tmp_path, monkeypatch
):
    db, _ = isolated_gateway_db
    source_path = tmp_path / "product-manual.pdf"
    source_path.write_bytes(b"pdf source that stays outside RAGFlow")
    monkeypatch.setenv("ENTERPRISE_FILE_SHARE_ROOTS", json.dumps({"test-root": str(tmp_path)}))
    payload = _payload(source_path)

    import enterprise.gateway.app as app_module

    app_module.app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/enterprise/api/v3/documents", json=payload
            )
    finally:
        app_module.app.dependency_overrides.pop(require_service_principal, None)
    assert response.status_code == 202
    accept = response.json()
    assert "statusUrl" not in accept
    assert accept["externalDocumentId"] == "DOC-FS"
    assert accept["sourceVersionId"] == "v1"
    assert accept["deduplicated"] is False

    class CapturingRAGFlow(RAGFlowDocumentStub):
        uploaded_handle = None
        uploaded_offset = None
        uploaded_bytes = None

        async def upload_document(
            self, dataset_id, file_name, file_content, request_id=None,
        ):
            self.uploaded_handle = file_content
            self.uploaded_offset = file_content.tell()
            self.uploaded_bytes = file_content.read()
            file_content.seek(0)
            return await super().upload_document(
                dataset_id, file_name, file_content, request_id,
            )

    ragflow = CapturingRAGFlow()
    assert not hasattr(ragflow, "register_external_document")
    assert not hasattr(ragflow, "refresh_external_document")
    service = SyncService(
        db,
        NeverReadSource(),
        ragflow,
        FileShareSourceAdapter(),
    )
    assert await OutboxWorker(service).run_once() == 1
    doc = await gw_read(db, get_mapping, "tenant-a", "DEMO", "DOC-FS", "v1")
    assert doc is not None
    assert doc.source_kind == "FILE_SHARE"
    assert doc.ragflow_document_id
    assert "upload" in ragflow._operation_log
    assert ragflow.uploaded_offset == 0
    assert ragflow.uploaded_bytes == source_path.read_bytes()
    assert ragflow.uploaded_handle.closed is True
    uploaded = ragflow._documents[doc.ragflow_document_id]["data"][0]
    assert "location" not in uploaded
    assert uploaded["meta_fields"]["enterprise_event_id"] == payload["eventId"]
    assert uploaded["meta_fields"]["equipment_id"] == "EQ-001"
    from enterprise.gateway.sync import v3_router

    monkeypatch.setattr(v3_router, "_sync_service", lambda _db: service)
    app_module.app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://test"
        ) as client:
            replay = await client.post("/enterprise/api/v3/documents", json=payload)
    finally:
        app_module.app.dependency_overrides.pop(require_service_principal, None)
    assert replay.status_code == 202
    assert replay.json()["deduplicated"] is True
    assert await OutboxWorker(service).run_once() == 0
    assert ragflow._operation_log.count("upload") == 1
    parse_count = len(ragflow._parse_calls)
    await service.reindex_document("tenant-a", "DEMO", "DOC-FS", "v1")
    assert len(ragflow._parse_calls) == parse_count + 1
    row = await gw_read(
        db,
        fetchone,
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_name='ext_source_ticket'",
    )
    assert row is None


@pytest.mark.asyncio
async def test_ragflow_client_uses_official_streaming_multipart(monkeypatch, tmp_path):
    source_path = tmp_path / "manual.pdf"
    source_path.write_bytes(b"streamed multipart")
    captured = {}

    import enterprise.gateway.sync.ragflow_document_client as client_module

    real_client = client_module.httpx.Client

    def handler(request):
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["Content-Type"]
        captured["body"] = request.read()
        return client_module.httpx.Response(
            200, json={"code": 0, "data": [{"id": "doc-1"}]},
        )

    transport = client_module.httpx.MockTransport(handler)

    def client(*, timeout):
        captured["timeout"] = timeout
        return real_client(timeout=timeout, transport=transport)

    monkeypatch.setattr(client_module.httpx, "Client", client)
    client = RAGFlowDocumentClient(
        base_url="http://ragflow.test", api_key="test-key",
    )
    with source_path.open("rb") as handle:
        result = await client.upload_document("dataset-1", source_path.name, handle)
        assert handle.tell() == source_path.stat().st_size

    assert result["code"] == 0
    assert captured["url"] == (
        "http://ragflow.test/api/v1/datasets/dataset-1/documents"
    )
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert (
        b'Content-Disposition: form-data; name="file"; filename="manual.pdf"'
        in captured["body"]
    )
    assert b"streamed multipart" in captured["body"]


@pytest.mark.asyncio
async def test_ragflow_parse_uses_official_dataset_chunks(monkeypatch):
    calls = []
    client = RAGFlowDocumentClient(
        base_url="http://ragflow.test", api_key="test-key",
    )

    def request(method, path, request_id, json_data=None):
        calls.append((method, path, json_data))
        return {"code": 0, "data": True}

    monkeypatch.setattr(client, "_sync_request", request)
    await client.start_parsing("dataset-1", ["doc-1"])
    assert calls == [
        ("POST", "/api/v1/datasets/dataset-1/chunks", {"document_ids": ["doc-1"]})
    ]


@pytest.mark.asyncio
async def test_file_share_hash_mismatch_fails_before_upload(
    isolated_gateway_db, tmp_path, monkeypatch
):
    db, _ = isolated_gateway_db
    source_path = tmp_path / "changed.pdf"
    source_path.write_bytes(b"changed content")
    monkeypatch.setenv(
        "ENTERPRISE_FILE_SHARE_ROOTS",
        json.dumps({"test-root": str(tmp_path)}),
    )
    payload = _payload(source_path, event_id="evt-hash", document_id="DOC-HASH")
    payload["sha256"] = "0" * 64

    import enterprise.gateway.app as app_module

    app_module.app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://test"
        ) as client:
            response = await client.post("/enterprise/api/v3/documents", json=payload)
    finally:
        app_module.app.dependency_overrides.pop(require_service_principal, None)
    assert response.status_code == 202

    ragflow = RAGFlowDocumentStub()
    service = SyncService(db, NeverReadSource(), ragflow, FileShareSourceAdapter())
    assert await OutboxWorker(service).run_once() == 1
    doc = await gw_read(db, get_mapping, "tenant-a", "DEMO", "DOC-HASH", "v1")
    assert doc.sync_status == "failed"
    assert doc.last_error_code == "DOCUMENT_HASH_MISMATCH"
    assert ragflow._documents == {}


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/enterprise/api/v1/citations/c1/source",
            "headers": headers,
        }
    )


@pytest.mark.asyncio
async def test_source_response_supports_exact_range_and_if_range(
    tmp_path, monkeypatch
):
    source_path = tmp_path / "range.pdf"
    source_path.write_bytes(b"0123456789")
    monkeypatch.setenv("ENTERPRISE_FILE_SHARE_ROOTS", json.dumps({"test-root": str(tmp_path)}))
    stat = FileShareSourceAdapter().stat_source("test-root", source_path.name)
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-FS",
        source_version_id="v1",
        event_id="evt-range",
        sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        file_name="设备调试记录.pdf",
        source_kind="FILE_SHARE",
        storage_root_id="test-root",
        relative_path=source_path.name,
        source_size=stat.size,
        source_modified_ns=stat.modified_ns,
        source_etag=stat.etag,
    )
    response = await source_response(
        _request([(b"range", b"bytes=2-5"), (b"if-range", stat.etag.encode())]), doc
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert response.status_code == 206
    assert body == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    full_response = await source_response(
        _request([(b"range", b"bytes=2-5"), (b"if-range", b'"stale"')]), doc
    )
    full_body = b"".join([chunk async for chunk in full_response.body_iterator])
    assert full_response.status_code == 200
    assert full_body == b"0123456789"
    assert parse_single_range("bytes=-3", 10) == (7, 9)
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range("bytes=bad", 10)


def test_file_share_external_contract_is_strict_and_matches_registered_routes():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts/file-share-v3.yaml").read_text(encoding="utf-8")
    )
    assert contract["openapi"] == "3.0.3"
    assert {
        path for path in contract["paths"]
        if path.startswith("/enterprise/api/")
    } == {
        "/enterprise/api/v3/documents",
        "/enterprise/api/v3/documents/sync-status",
        "/enterprise/api/v3/documents/{externalDocumentId}/status",
    }
    assert contract["info"]["version"] == "3.1.0"
    register = contract["paths"]["/enterprise/api/v3/documents"]["post"]
    assert register["security"] == [{"HmacSignature": []}]
    assert register["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentAccepted"
    }
    request_schema = contract["components"]["schemas"]["DocumentUpsertRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["mediaType"]["enum"] == ["application/pdf"]
    accept_schema = contract["components"]["schemas"]["DocumentAccepted"]
    assert set(accept_schema["required"]) == {
        "operationId",
        "externalDocumentId",
        "sourceVersionId",
        "deduplicated",
        "updatedAt",
    }
    assert "statusUrl" not in accept_schema["properties"]
    status_schema = contract["components"]["schemas"]["DocumentStatus"]
    assert {
        "statusUrl",
        "pipelineStatus",
        "parseCompleted",
        "indexCompleted",
        "qualityStatus",
        "retrievable",
        "errorCode",
    } <= set(status_schema["required"])
    assert "ragflowDocumentId" not in status_schema["properties"]
    callback = yaml.safe_load(
        (REPO_ROOT / "contracts/file-share-callback-v1.yaml").read_text(encoding="utf-8")
    )
    assert callback["info"]["version"] == "1.0.0"
    callback_schema = callback["components"]["schemas"]["FileShareTerminalCallback"]
    assert {
        "deliveryId",
        "eventType",
        "originatingEventId",
        "externalDocumentId",
        "sourceVersionId",
        "status",
        "timestamp",
        "payloadVersion",
    } <= set(callback_schema["required"])
