"""Message-attachment contract and runtime checks for inquiry v2.3."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.audit_log import _parse_body
from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config
from enterprise.gateway.feed_audit_middleware import FeedRegisterAuditMiddleware
from enterprise.gateway.query import formal_router, v2_router, v2_store
from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub
from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping
from enterprise.gateway.sync.transient_attachment import get_storage
from enterprise.tests.test_transient_attachment import MemoryObjectStorage

BASE = "/enterprise/api/v2"
ROOT = Path(__file__).resolve().parents[2]
MIN_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _principal() -> UserPrincipal:
    return UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )


@pytest.fixture
def runtime(isolated_gateway_db, monkeypatch):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
    monkeypatch.setenv("S3_BUCKET", "attachment-test-bucket")
    storage = MemoryObjectStorage()
    application = FastAPI()
    application.add_middleware(FeedRegisterAuditMiddleware)
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_user_principal] = _principal
    application.dependency_overrides[get_storage] = lambda: storage
    stub = RAGFlowQueryStub()
    formal_router._query_stub = stub
    value = type("Runtime", (), {"app": application, "db": db, "stub": stub, "storage": storage})()
    try:
        yield value
    finally:
        formal_router._query_stub = None


def _client(runtime) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test")


async def _seed_doc(db) -> None:
    await insert_mapping(
        db,
        ExtDocumentMap(
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-ATT",
            source_version_id="v1",
            event_id=str(uuid.uuid4()),
            sha256=hashlib.sha256(b"DOC-ATT").hexdigest(),
            file_name="DOC-ATT.pdf",
            asset_id="FA-ATT",
            equipment_id="EQ-ATT",
            fixed_asset_no="FA-ATT",
            department_id="d10",
            security_level=2,
            allow_group_ids='["maintenance"]',
            deny_group_ids="[]",
            ragflow_dataset_id="ds-v2",
            ragflow_document_id="doc-1",
            sync_status="ready",
            pipeline_status="DONE",
            business_status="active",
            current_version=1,
        ),
    )


def test_openapi_23_declares_multipart_and_history_metadata():
    spec = yaml.safe_load(
        (ROOT / "contracts" / "integration-openapi-v2.yaml").read_text(encoding="utf-8")
    )
    assert spec["info"]["version"] == "2.3.0"
    post = spec["paths"]["/conversations/{conversationId}/messages"]["post"]
    assert "application/json" in post["requestBody"]["content"]
    assert "multipart/form-data" in post["requestBody"]["content"]
    assert "413" in post["responses"]
    message = spec["components"]["schemas"]["Message"]
    assert "attachments" in message["properties"]
    attachment = spec["components"]["schemas"]["MessageAttachment"]
    assert set(attachment["required"]) >= {
        "attachmentId",
        "fileName",
        "mediaType",
        "sizeBytes",
        "sha256",
    }
    assert "content" not in attachment["properties"]
    assert set(attachment["properties"]["mediaType"]["enum"]) == {
        "image/jpeg",
        "image/png",
        "text/plain",
        "application/pdf",
    }


def test_parse_body_redacts_base64_content():
    parsed = _parse_body(
        json.dumps({"fileName": "a.png", "content": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"})
    )
    assert parsed["fileName"] == "a.png"
    assert parsed["content"] == "<redacted>"


def test_json_question_still_validates():
    req = v2_router.CreateMessageRequest.model_validate(
        {"clientMessageId": "m-json", "question": "hello"}
    )
    assert req.question == "hello"


def test_json_attachments_content_is_rejected():
    with pytest.raises(Exception):
        v2_router.CreateMessageRequest.model_validate(
            {
                "clientMessageId": "m-b64",
                "question": "hello",
                "attachments": [{"content": "iVBORw0KGgo"}],
            }
        )


@pytest.mark.asyncio
async def test_json_question_still_works(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        assert created.status_code == 201
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            json={"clientMessageId": "plain-1", "question": "维护步骤"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_json_attachments_content_returns_422(runtime):
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            json={
                "clientMessageId": "b64-1",
                "question": "q",
                "attachments": [{"content": "iVBORw0KGgo"}],
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_multipart_png_only_enriches_and_deletes_ragflow_file(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        conversation_id = created.json()["conversationId"]
        response = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            data={"metadata": json.dumps({"clientMessageId": "png-only"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
        history = await client.get(f"{BASE}/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    assert "E07" in body["question"]
    assert not body.get("files")
    items = history.json()["items"]
    user = next(item for item in items if item["role"] == "user")
    assert user["content"] == ""
    assert user["attachments"][0]["fileName"] == "photo.png"
    assert "content" not in user["attachments"][0]
    assert "downloadUrl" not in user["attachments"][0]
    async with runtime.db.execute("SELECT COUNT(*) AS n FROM ext_document_map") as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1
    assert runtime.stub.deleted_files


@pytest.mark.asyncio
async def test_multipart_txt_history_keeps_filename(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        conversation_id = created.json()["conversationId"]
        response = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            data={
                "metadata": json.dumps(
                    {"clientMessageId": "txt-1", "question": "看看这个说明"}
                )
            },
            files={"files": ("note.txt", b"hello txt", "text/plain")},
        )
        history = await client.get(f"{BASE}/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    user = next(item for item in history.json()["items"] if item["role"] == "user")
    assert user["content"] == "看看这个说明"
    assert user["attachments"][0]["fileName"] == "note.txt"


@pytest.mark.asyncio
async def test_chips_with_files_are_rejected(runtime):
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={
                "metadata": json.dumps(
                    {
                        "clientMessageId": "chip-file",
                        "suggestionId": "maintenance",
                        "contextVersion": 1,
                    }
                )
            },
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_docx_is_rejected(runtime):
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "docx-1"})},
            files={
                "files": (
                    "a.docx",
                    b"PK\x03\x04",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_replay_does_not_create_second_attachment(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        conversation_id = created.json()["conversationId"]
        payload = {
            "data": {"metadata": json.dumps({"clientMessageId": "replay-1"})},
            "files": {"files": ("photo.png", MIN_PNG, "image/png")},
        }
        first = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages", **payload
        )
        second = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages", **payload
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    async with runtime.db.execute(
        "SELECT COUNT(*) AS n FROM ext_transient_attachment"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_disabled_flag_returns_503(runtime, monkeypatch):
    monkeypatch.setattr(config, "transient_attachments_enabled", False)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "off-1"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_multipart_png_is_not_written_to_audit_log(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "audit-png"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    text = (tmp_path / "inquiry.jsonl").read_text(encoding="utf-8")
    assert "iVBORw0" not in text
    assert "%PDF" not in text
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    message_records = [item for item in records if item["path"].endswith("/messages")]
    assert message_records
    body = message_records[-1]["body"]
    assert body["clientMessageId"] == "audit-png"
    assert body["hasAttachments"] is True
