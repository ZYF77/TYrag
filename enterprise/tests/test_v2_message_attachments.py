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
from enterprise.gateway.query import formal_router, v2_router
from enterprise.gateway.query.attachment_context import (
    DOCX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    PendingAttachment,
    chat_is_vision_capable,
    completion_files,
    decode_content_disposition_filename,
    ragflow_attachment_filename,
)
from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE
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
    assert spec["info"]["version"] == "2.9.0"
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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    files = body.get("files") or []
    assert any(item.get("mime_type") == "image/png" for item in files)
    understand = runtime.stub._last_understand_file
    assert understand is not None
    assert understand["mime_type"] == "image/png"
    assert understand["name"] == "photo.png"
    assert understand["id"]
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
    async with runtime.db.execute(
        "SELECT file_id, deleted_at FROM ext_ragflow_temp_file"
    ) as cursor:
        temps = await cursor.fetchall()
    assert temps
    assert all(row["deleted_at"] for row in temps)


@pytest.mark.asyncio
async def test_multipart_txt_history_keeps_filename(runtime):
    await _seed_doc(runtime.db)
    await runtime.stub.create_chat(
        "enterprise-formal-customer-a",
        ["ds-v2"],
        prompt_config={
            "web_search_provider": "tavily",
            "tavily_api_key": "test-key",
        },
    )
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        conversation_id = created.json()["conversationId"]
        response = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            data={
                "metadata": json.dumps(
                    {
                        "clientMessageId": "txt-1",
                        "question": "看看这个说明",
                        "internetEnabled": True,
                    }
                )
            },
            files={"files": ("note.txt", b"hello txt", "text/plain")},
        )
        history = await client.get(f"{BASE}/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    user = next(item for item in history.json()["items"] if item["role"] == "user")
    assert user["content"] == "看看这个说明"
    assert user["attachments"][0]["fileName"] == "note.txt"
    body = runtime.stub._last_completion_body
    assert body is not None
    assert body["internet"] is True
    files = body.get("files") or []
    assert files
    assert files[0]["mime_type"] == "text/plain"


RFC2047_TXT = "=?utf-8?B?5paw5paH5Lu2Mi50eHQ=?="
RFC2047_PDF = "=?utf-8?B?6K6+5aSH5Zu+57q4LnBkZg==?="


@pytest.mark.asyncio
async def test_rfc2047_txt_filename_is_decoded_for_ragflow(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        conversation_id = created.json()["conversationId"]
        response = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            data={
                "metadata": json.dumps(
                    {"clientMessageId": "rfc2047-txt", "question": "里面有哪些信息？"}
                )
            },
            files={"files": (RFC2047_TXT, b"fault code E07 in txt", "text/plain")},
        )
        history = await client.get(f"{BASE}/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    user = next(item for item in history.json()["items"] if item["role"] == "user")
    assert user["attachments"][0]["fileName"] == "新文件2.txt"
    assert runtime.stub.uploaded_files
    uploaded = runtime.stub.uploaded_files[-1]
    assert uploaded.endswith(".txt")
    assert "=?" not in uploaded
    files = (runtime.stub._last_completion_body or {}).get("files") or []
    assert files
    assert files[0]["name"].endswith(".txt")
    assert "=?" not in files[0]["name"]


@pytest.mark.asyncio
async def test_rfc2047_pdf_filename_is_decoded_for_ragflow(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={
                "metadata": json.dumps(
                    {"clientMessageId": "rfc2047-pdf", "question": "这个图纸内容描述下"}
                )
            },
            files={"files": (RFC2047_PDF, b"%PDF-1.4 fake", "application/pdf")},
        )
    assert response.status_code == 200, response.text
    uploaded = runtime.stub.uploaded_files[-1]
    assert uploaded.endswith(".pdf")
    assert uploaded == "设备图纸.pdf"
    files = (runtime.stub._last_completion_body or {}).get("files") or []
    assert files[0]["name"].endswith(".pdf")
    assert files[0]["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_multipart_txt_works_without_object_storage(isolated_gateway_db, monkeypatch):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_TRANSIENT_BUCKET", raising=False)

    async def boom_storage():
        raise RuntimeError("object storage should not be used for message attachments")

    application = FastAPI()
    application.add_middleware(FeedRegisterAuditMiddleware)
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_user_principal] = _principal
    application.dependency_overrides[get_storage] = boom_storage
    stub = RAGFlowQueryStub()
    formal_router._query_stub = stub
    await _seed_doc(db)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
            response = await client.post(
                f"{BASE}/conversations/{created.json()['conversationId']}/messages",
                data={
                    "metadata": json.dumps(
                        {"clientMessageId": "no-s3-txt", "question": "看看这个说明"}
                    )
                },
                files={"files": ("note.txt", b"HMI fault code E07", "text/plain")},
            )
            history = await client.get(
                f"{BASE}/conversations/{created.json()['conversationId']}/messages"
            )
    finally:
        formal_router._query_stub = None
    assert response.status_code == 200, response.text
    atts = response.json().get("attachments") or []
    assert atts
    assert "content" not in atts[0]
    assert "downloadUrl" not in atts[0]
    user = next(item for item in history.json()["items"] if item["role"] == "user")
    assert user["attachments"][0]["fileName"] == "note.txt"
    async with db.execute("SELECT COUNT(*) AS n FROM ext_transient_attachment") as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 0


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
async def test_docx_is_accepted(runtime):
    await _seed_doc(runtime.db)
    office_body = b"PK\x03\x04SECRET_OFFICE_BODY_SHOULD_NOT_APPEAR"
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "docx-1"})},
            files={"files": ("a.docx", office_body, DOCX_MEDIA_TYPE)},
        )
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    files = body.get("files") or []
    assert files
    assert files[0]["mime_type"] == DOCX_MEDIA_TYPE
    assert "SECRET_OFFICE_BODY_SHOULD_NOT_APPEAR" not in (body.get("question") or "")


@pytest.mark.asyncio
async def test_xlsx_is_accepted(runtime):
    await _seed_doc(runtime.db)
    office_body = b"PK\x03\x04SECRET_SHEET_BODY_SHOULD_NOT_APPEAR"
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "xlsx-1"})},
            files={"files": ("a.xlsx", office_body, XLSX_MEDIA_TYPE)},
        )
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    files = body.get("files") or []
    assert files
    assert files[0]["mime_type"] == XLSX_MEDIA_TYPE
    assert "SECRET_SHEET_BODY_SHOULD_NOT_APPEAR" not in (body.get("question") or "")
    assert runtime.stub.understand_calls == 0


@pytest.mark.asyncio
async def test_legacy_doc_is_rejected(runtime):
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "doc-1"})},
            files={"files": ("a.doc", b"legacy-ole", "application/msword")},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_powerpoint_is_rejected(runtime):
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "ppt-1"})},
            files={
                "files": (
                    "a.ppt",
                    b"legacy-ppt",
                    "application/vnd.ms-powerpoint",
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
        history = await client.get(f"{BASE}/conversations/{conversation_id}/messages")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert runtime.stub.uploaded_files.count("photo.png") == 1
    assert runtime.stub.understand_calls == 1
    async with runtime.db.execute(
        "SELECT COUNT(*) AS n FROM ext_transient_attachment"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 0
    user = next(item for item in history.json()["items"] if item["role"] == "user")
    assert len(user["attachments"]) == 1
    assert user["attachments"][0]["fileName"] == "photo.png"


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


def test_rfc2047_filename_helpers():
    assert decode_content_disposition_filename(RFC2047_TXT) == "新文件2.txt"
    assert decode_content_disposition_filename(RFC2047_PDF) == "设备图纸.pdf"
    assert ragflow_attachment_filename(RFC2047_TXT, "text/plain") == "新文件2.txt"
    assert ragflow_attachment_filename(RFC2047_PDF, "application/pdf") == "设备图纸.pdf"
    assert ragflow_attachment_filename("note.txt", "text/plain") == "note.txt"
    assert ragflow_attachment_filename("report", "application/pdf") == "report.pdf"
    assert ragflow_attachment_filename("photo.JPEG", "image/jpeg") == "photo.JPEG"


def test_text_chat_omits_images_from_completion_files():
    item = PendingAttachment(
        file_name="photo.png",
        media_type="image/png",
        content=MIN_PNG,
        sha256="a" * 64,
        size_bytes=len(MIN_PNG),
        ragflow_file={"id": "rf-1", "mime_type": "image/png", "name": "photo.png"},
    )
    office = PendingAttachment(
        file_name="a.docx",
        media_type=DOCX_MEDIA_TYPE,
        content=b"PK",
        sha256="b" * 64,
        size_bytes=2,
        ragflow_file={"id": "rf-2", "mime_type": DOCX_MEDIA_TYPE, "name": "a.docx"},
    )
    assert completion_files([item], vision=False) == []
    assert completion_files([item], vision=True) == [item.ragflow_file]
    assert completion_files([item, office], vision=False) == [office.ragflow_file]


def test_chat_is_vision_capable_defaults_for_chat_and_missing(monkeypatch):
    monkeypatch.delenv("ENTERPRISE_CHAT_PASS_IMAGES", raising=False)
    assert chat_is_vision_capable({"llm_setting": {"model_type": "chat"}}) is True
    assert chat_is_vision_capable({"llm_setting": {"model_type": "vision"}}) is True
    assert chat_is_vision_capable({"llm_setting": {"model_type": "image2text"}}) is True
    assert chat_is_vision_capable({"llm_setting": {"model_type": "img2txt"}}) is True
    assert chat_is_vision_capable({"llm_setting": {"model_type": ["chat"]}}) is True
    assert chat_is_vision_capable({"llm_id": "gpt-4o"}) is True
    assert chat_is_vision_capable(None) is True
    assert chat_is_vision_capable({"llm_id": "plain-llm-no-vl"}) is True


@pytest.mark.parametrize("raw", ("0", "false", "no", "FALSE", "No"))
def test_chat_is_vision_capable_env_off(monkeypatch, raw):
    monkeypatch.setenv("ENTERPRISE_CHAT_PASS_IMAGES", raw)
    assert chat_is_vision_capable({"llm_setting": {"model_type": "chat"}}) is False
    assert chat_is_vision_capable({"llm_setting": {"model_type": "vision"}}) is False
    assert chat_is_vision_capable({"llm_id": "gpt-4o"}) is False


@pytest.mark.asyncio
async def test_chat_model_includes_png_in_final_files(runtime):
    runtime.stub.default_llm_setting = {"model_type": "chat"}
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "png-chat"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    assert "E07" in body["question"]
    files = body.get("files") or []
    assert any(item.get("mime_type") == "image/png" for item in files)


@pytest.mark.asyncio
async def test_vision_chat_includes_png_in_final_files(runtime):
    runtime.stub.default_llm_setting = {"model_type": "vision"}
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "png-vision"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    assert "E07" in body["question"]
    files = body.get("files") or []
    assert any(item.get("mime_type") == "image/png" for item in files)


@pytest.mark.asyncio
async def test_env_disables_png_in_final_files(runtime, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_CHAT_PASS_IMAGES", "0")
    runtime.stub.default_llm_setting = {"model_type": "chat"}
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "png-text"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body is not None
    assert "E07" in body["question"]
    files = body.get("files") or []
    assert not any(str(item.get("mime_type") or "").startswith("image/") for item in files)


@pytest.mark.asyncio
async def test_vision_sse_sends_png_files(runtime):
    runtime.stub.default_llm_setting = {"model_type": "vision"}
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={"metadata": json.dumps({"clientMessageId": "png-sse"})},
            files={"files": ("photo.png", MIN_PNG, "image/png")},
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200, response.text
        async for _ in response.aiter_bytes():
            pass
    body = runtime.stub._last_completion_body
    assert body is not None
    files = body.get("files") or []
    assert any(item.get("mime_type") == "image/png" for item in files)


@pytest.mark.asyncio
async def test_image_observation_without_abstain_is_completed(runtime):
    runtime.stub.forced_answer = "从你上传的附件中识别到疑似 E07。"
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={
                "metadata": json.dumps(
                    {"clientMessageId": "obs-1", "question": "图里是什么"}
                )
            },
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "已完成"
    assert body["citations"] == []
    assert ABSTAIN_PHRASE not in (body.get("answer") or "")


@pytest.mark.asyncio
async def test_repair_question_without_kb_still_abstains(runtime):
    runtime.stub.forced_answer = f"附件只是观察。{ABSTAIN_PHRASE}"
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(f"{BASE}/conversations", json={"equipmentId": "EQ-ATT"})
        response = await client.post(
            f"{BASE}/conversations/{created.json()['conversationId']}/messages",
            data={
                "metadata": json.dumps(
                    {"clientMessageId": "repair-1", "question": "维修步骤是什么"}
                )
            },
            files={"files": ("photo.png", MIN_PNG, "image/png")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "无可靠依据"
    assert body.get("answer") == formal_router.NO_RELIABLE_EVIDENCE_ANSWER


def test_upstream_empty_response_skips_when_last_message_has_files():
    source = (
        ROOT / "ragflow" / "api" / "db" / "services" / "dialog_service.py"
    ).read_text(encoding="utf-8")
    assert (
        'if not knowledges and prompt_config.get("empty_response") '
        'and not messages[-1].get("files"):'
    ) in source
    assert (
        'if not knowledges and prompt_config.get("empty_response"):\n'
    ) not in source
