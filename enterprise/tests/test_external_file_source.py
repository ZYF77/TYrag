from __future__ import annotations

import ast
import hashlib
import json
import urllib.request
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
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceAdapter
from enterprise.gateway.sync.sync_service import SyncService
from enterprise.gateway.sync.worker import OutboxWorker
from ragflow.rag.utils.external_source import fetch_external_source


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


@pytest.mark.asyncio
async def test_file_share_ticket_is_one_shot_and_does_not_store_bytes(
    isolated_gateway_db, tmp_path, monkeypatch
):
    db, _ = isolated_gateway_db
    source_path = tmp_path / "设备调试记录.pdf"
    source_path.write_bytes(b"authoritative pdf bytes")
    monkeypatch.setenv("ENTERPRISE_FILE_SHARE_ROOTS", json.dumps({"test-root": str(tmp_path)}))
    provider = FileShareSourceAdapter()
    ticket = await provider.issue_ticket(
        db,
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-FS",
        source_version_id="v1",
        storage_root_id="test-root",
        relative_path=source_path.name,
        file_name=source_path.name,
        media_type="application/pdf",
        expected_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )

    import enterprise.gateway.app as app_module

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        first = await client.get(f"/enterprise/internal/source-tickets/{ticket.token}")
        second = await client.get(f"/enterprise/internal/source-tickets/{ticket.token}")
    assert first.status_code == 200
    assert first.content == source_path.read_bytes()
    assert first.headers["x-source-sha256"] == hashlib.sha256(first.content).hexdigest()
    assert "filename*=UTF-8''" in first.headers["content-disposition"]
    assert second.status_code == 404
    async with db.execute("PRAGMA table_info(ext_source_ticket)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    assert "content" not in columns
    with pytest.raises(FileShareConfigurationError):
        provider.resolve_path("test-root", "../manual.pdf")


@pytest.mark.asyncio
async def test_v3_file_share_worker_registers_virtual_document_without_upload(
    isolated_gateway_db, tmp_path, monkeypatch
):
    db, _ = isolated_gateway_db
    source_path = tmp_path / "product-manual.pdf"
    source_path.write_bytes(b"pdf source that stays outside RAGFlow")
    monkeypatch.setenv("ENTERPRISE_FILE_SHARE_ROOTS", json.dumps({"test-root": str(tmp_path)}))

    import enterprise.gateway.app as app_module

    app_module.app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/enterprise/api/v3/documents", json=_payload(source_path)
            )
    finally:
        app_module.app.dependency_overrides.pop(require_service_principal, None)
    assert response.status_code == 202
    accept = response.json()
    assert "statusUrl" not in accept
    assert accept["externalDocumentId"] == "DOC-FS"
    assert accept["sourceVersionId"] == "v1"
    assert accept["deduplicated"] is False

    ragflow = RAGFlowDocumentStub()
    service = SyncService(
        db,
        NeverReadSource(),
        ragflow,
        FileShareSourceAdapter(),
    )
    assert await OutboxWorker(service).run_once() == 1
    doc = await get_mapping(db, "tenant-a", "DEMO", "DOC-FS", "v1")
    assert doc is not None
    assert doc.source_kind == "FILE_SHARE"
    assert doc.ragflow_document_id
    assert "external_register" in ragflow._operation_log
    assert "upload" not in ragflow._operation_log
    assert ragflow._documents[doc.ragflow_document_id]["data"][0]["location"].startswith(
        "external://"
    )
    async with db.execute("SELECT COUNT(*) AS count FROM ext_source_ticket") as cursor:
        assert (await cursor.fetchone())["count"] == 1


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


def test_ragflow_external_reader_verifies_hash_and_uses_temp_storage(monkeypatch):
    chunks = [b"temporary ", b"source"]

    class FakeResponse:
        headers = {"Content-Length": "16", "X-Source-SHA256": hashlib.sha256(b"temporary source").hexdigest()}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _size):
            return chunks.pop(0) if chunks else b""

    monkeypatch.setenv("TYRAG_EXTERNAL_SOURCE_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    assert fetch_external_source("external://one-shot-ticket") == b"temporary source"


def _function_node(path: Path, name: str) -> tuple[ast.AsyncFunctionDef | ast.FunctionDef, str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node, source
    raise AssertionError(f"missing function {name} in {path}")


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_ragflow_external_entrypoints_are_registered_and_prefer_ticket_fetch():
    document_api = REPO_ROOT / "ragflow/api/apps/restful_apis/document_api.py"
    register, source = _function_node(document_api, "register_external_document")
    refresh, _ = _function_node(document_api, "refresh_external_document_source")
    register_source = "\n".join(source.splitlines()[register.lineno - 1 : register.end_lineno])

    assert "/datasets/<dataset_id>/documents/external" in _string_literals(register)
    assert "/datasets/<dataset_id>/documents/<document_id>/external-source" in _string_literals(refresh)
    register_literals = _string_literals(register)
    assert "external://" in register_literals
    assert "enterprise_file_share" in register_literals
    assert "location" in register_literals
    assert "FileType.VIRTUAL" in register_source
    assert "DocumentService.insert" in register_source
    assert "FileService.add_file_from_kb" in register_source


@pytest.mark.parametrize(
    ("relative_path", "function_name"),
    [
        ("ragflow/rag/svr/task_executor.py", "get_storage_binary"),
        (
            "ragflow/rag/svr/task_executor_refactor/task_handler.py",
            "_get_storage_binary",
        ),
    ],
)
def test_ragflow_task_executors_route_external_locations_before_storage(
    relative_path: str, function_name: str
):
    node, source = _function_node(REPO_ROOT / relative_path, function_name)
    function_source = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
    assert 'startswith("external://")' in function_source
    assert "fetch_external_source" in function_source
    assert function_source.index("fetch_external_source") < function_source.index("STORAGE_IMPL.get")


def test_file_share_contract_is_strict_and_matches_the_registered_routes():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts/file-share-v3.yaml").read_text(encoding="utf-8")
    )
    assert contract["openapi"] == "3.0.3"
    assert set(contract["paths"]) == {
        "/enterprise/api/v3/documents",
        "/enterprise/api/v3/documents/sync-status",
        "/enterprise/api/v3/documents/{externalDocumentId}/status",
        "/enterprise/internal/source-tickets/{ticket}",
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
