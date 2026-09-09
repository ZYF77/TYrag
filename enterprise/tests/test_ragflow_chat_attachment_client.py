"""Unit tests for chat-attachment upload / understand wire format."""

from __future__ import annotations

import json

import pytest

from enterprise.gateway.query.ragflow_client import RAGFlowQueryClient


@pytest.mark.asyncio
async def test_upload_chat_file_multipart_reaches_documents_upload(monkeypatch):
    """Regression: upload_chat_file must pass files= that _sync_request accepts."""
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "id": "att-1",
                        "name": "paste.png",
                        "mime_type": "image/png",
                        "created_by": "tenant-1",
                        "size": 9,
                    },
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["content_type"] = req.headers.get("Content-type") or req.headers.get(
            "Content-Type"
        )
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    desc = await client.upload_chat_file("paste.png", b"png-bytes", "image/png")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/v1/documents/upload")
    assert captured["url"].endswith("/api/v1/files") is False
    assert "multipart/form-data" in str(captured["content_type"])
    assert b"png-bytes" in captured["body"]
    assert desc["id"] == "att-1"
    assert desc["mime_type"] == "image/png"
    assert desc["created_by"] == "tenant-1"
    assert desc["name"] == "paste.png"


@pytest.mark.asyncio
async def test_upload_chat_file_uses_documents_upload_not_file_manager(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["method"] = method
        captured["path"] = path
        captured["files"] = files
        return {
            "code": 0,
            "data": {
                "id": "att-1",
                "name": "paste.png",
                "mime_type": "image/png",
                "created_by": "tenant-1",
                "size": 12,
            },
        }

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    desc = await client.upload_chat_file("paste.png", b"png-bytes", "image/png")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/documents/upload"
    assert captured["path"] != "/api/v1/files"
    assert isinstance(desc, dict)
    assert desc["id"] == "att-1"
    assert desc["mime_type"] == "image/png"
    assert desc["created_by"] == "tenant-1"
    assert desc["name"] == "paste.png"


@pytest.mark.asyncio
async def test_delete_chat_file_uses_authenticated_upload_resource(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured.update(method=method, path=path, json=json_data, files=files)
        return {"code": 0, "data": {"id": "a" * 32}}

    monkeypatch.setattr(client, "_sync_request", fake_sync)

    await client.delete_file("a" * 32, created_by="untrusted-tenant")

    assert captured == {
        "method": "DELETE",
        "path": f"/api/v1/documents/upload/{'a' * 32}",
        "json": None,
        "files": None,
    }


@pytest.mark.asyncio
async def test_understand_file_passes_attachment_descriptor_not_bare_id(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    async def fake_completion(
        chat_id,
        question,
        session_id=None,
        doc_ids=None,
        request_id=None,
        files=None,
        llm_id=None,
        timeout=None,
        **kwargs,
    ):
        del kwargs
        captured["chat_id"] = chat_id
        captured["session_id"] = session_id
        captured["files"] = files
        captured["question"] = question
        captured["llm_id"] = llm_id
        captured["timeout"] = timeout
        return {
            "code": 0,
            "data": {
                "answer": '{"errorCodes":["E07"],"textSpans":[],"equipmentCodes":[],"visibleValues":[],"confidence":0.9}',
            },
        }

    monkeypatch.setattr(client, "chat_completion", fake_completion)
    desc = {
        "id": "att-1",
        "name": "paste.png",
        "mime_type": "image/png",
        "created_by": "tenant-1",
    }
    parsed = await client.understand_file("chat-should-be-ignored", desc)
    assert captured["chat_id"] is None
    assert captured["session_id"] is None
    assert isinstance(captured["files"], list)
    assert len(captured["files"]) == 1
    assert captured["files"][0]["id"] == "att-1"
    assert captured["files"][0]["mime_type"] == "image/png"
    assert captured["files"][0]["created_by"] == "tenant-1"
    assert "知识库" in captured["question"] or "禁止" in captured["question"]
    assert parsed["errorCodes"] == ["E07"]


@pytest.mark.asyncio
async def test_chat_completion_omits_chat_id_when_none(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["json"] = json_data
        return {"code": 0, "data": {"answer": "ok", "reference": {"chunks": []}}}

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    await client.chat_completion(
        None,
        "see image",
        files=[{
            "id": "att-1",
            "name": "paste.png",
            "mime_type": "image/png",
            "created_by": "tenant-1",
        }],
    )
    assert "chat_id" not in captured["json"]
    assert "internet" not in captured["json"]
    assert captured["json"]["files"][0]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_create_named_session_uses_public_chat_api(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured.update(method=method, path=path, json=json_data)
        return {"code": 0, "data": {"id": "session-1", "name": json_data["name"]}}

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    result = await client.create_session("chat-1", "eam-user-conversation-run")
    assert captured == {
        "method": "POST",
        "path": "/api/v1/chats/chat-1/sessions",
        "json": {"name": "eam-user-conversation-run"},
    }
    assert result["data"]["id"] == "session-1"



@pytest.mark.asyncio
async def test_chat_completion_forwards_file_descriptors(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["path"] = path
        captured["json"] = json_data
        return {"code": 0, "data": {"answer": "ok", "reference": {"chunks": []}}}

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    desc = {
        "id": "att-1",
        "name": "paste.png",
        "mime_type": "image/png",
        "created_by": "tenant-1",
    }
    await client.chat_completion(
        "chat-1", "see image", files=[desc], internet=True
    )
    assert captured["path"] == "/api/v1/chat/completions"
    assert captured["json"]["files"] == [desc]
    assert captured["json"]["internet"] is True
    assert not isinstance(captured["json"]["files"][0], str)


@pytest.mark.asyncio
async def test_chat_completion_stream_forwards_file_descriptors(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    class _Resp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield 'data: {"code":0,"data":true}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Http:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Http)
    desc = {
        "id": "att-1",
        "name": "paste.png",
        "mime_type": "image/png",
        "created_by": "tenant-1",
    }
    payloads = []
    async for payload in client.chat_completion_stream(
        "chat-1", "see image", files=[desc], internet=True
    ):
        payloads.append(payload)
    assert captured["json"]["files"] == [desc]
    assert captured["json"]["internet"] is True
    assert captured["json"]["stream"] is True
    assert payloads and payloads[-1]["data"] is True


@pytest.mark.asyncio
async def test_understand_file_includes_configured_llm_id(monkeypatch):
    """Configured vision llm_id is forwarded; chat_id stays unset."""
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_VISION_LLM_ID", "vl-ocr@local")
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_VISION_TIMEOUT_SECONDS", "18")
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["path"] = path
        captured["json"] = json_data
        captured["timeout"] = timeout
        return {
            "code": 0,
            "data": {
                "answer": '{"errorCodes":["E07"],"textSpans":[],"equipmentCodes":[],"visibleValues":[],"confidence":0.9}',
            },
        }

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    desc = {
        "id": "att-1",
        "name": "paste.png",
        "mime_type": "image/png",
        "created_by": "tenant-1",
    }
    parsed = await client.understand_file("chat-should-be-ignored", desc)
    assert captured["path"] == "/api/v1/chat/completions"
    assert "chat_id" not in captured["json"]
    assert captured["json"].get("session_id") is None or "session_id" not in captured["json"]
    assert captured["json"]["llm_id"] == "vl-ocr@local"
    assert captured["json"]["files"][0]["id"] == "att-1"
    assert captured["timeout"] == 18.0
    assert parsed["errorCodes"] == ["E07"]


@pytest.mark.asyncio
async def test_understand_file_omits_llm_id_when_unset(monkeypatch):
    monkeypatch.delenv("ENTERPRISE_ATTACHMENT_VISION_LLM_ID", raising=False)
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_VISION_TIMEOUT_SECONDS", "30")
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["json"] = json_data
        captured["timeout"] = timeout
        return {
            "code": 0,
            "data": {
                "answer": '{"errorCodes":[],"textSpans":["x"],"equipmentCodes":[],"visibleValues":[],"confidence":0.5}',
            },
        }

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    desc = {
        "id": "att-2",
        "name": "paste.png",
        "mime_type": "image/png",
        "created_by": "tenant-1",
    }
    await client.understand_file(None, desc)
    assert "chat_id" not in captured["json"]
    assert "llm_id" not in captured["json"]
    assert captured["timeout"] == 30.0


@pytest.mark.asyncio
async def test_chat_completion_forwards_llm_id_and_timeout(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None, timeout=None):
        captured["json"] = json_data
        captured["timeout"] = timeout
        return {"code": 0, "data": {"answer": "ok", "reference": {"chunks": []}}}

    monkeypatch.setattr(client, "_sync_request", fake_sync)
    await client.chat_completion(
        None,
        "see image",
        files=[{
            "id": "att-1",
            "name": "paste.png",
            "mime_type": "image/png",
            "created_by": "tenant-1",
        }],
        llm_id="vl-ocr@local",
        timeout=12.5,
    )
    assert "chat_id" not in captured["json"]
    assert captured["json"]["llm_id"] == "vl-ocr@local"
    assert captured["timeout"] == 12.5


@pytest.mark.asyncio
async def test_observe_attachments_skips_understand_when_disabled(monkeypatch):
    from enterprise.gateway.query.attachment_context import (
        PendingAttachment,
        observe_attachments,
    )

    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_VISION_ENABLED", "false")
    calls: list = []

    class _Client:
        async def upload_chat_file(self, file_name, content, media_type):
            return {
                "id": "att-1",
                "name": file_name,
                "mime_type": media_type,
                "created_by": "tenant-1",
            }

        async def understand_file(self, chat_id, file, request_id=None):
            calls.append((chat_id, file))
            raise AssertionError("understand_file must not be called when disabled")

    pending = [
        PendingAttachment(
            file_name="paste.png",
            media_type="image/png",
            content=b"png",
            sha256="a" * 64,
            size_bytes=3,
        )
    ]
    observations = await observe_attachments(pending, _Client(), chat_id="chat-1")
    assert calls == []
    assert len(observations) == 1
    assert observations[0].understood is False
    assert observations[0].error_codes == []


@pytest.mark.asyncio
async def test_observe_attachments_degrades_on_understand_timeout(monkeypatch):
    from enterprise.gateway.query.attachment_context import (
        PendingAttachment,
        observe_attachments,
    )
    from enterprise.gateway.query.ragflow_client import RAGFlowAPIError

    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_VISION_ENABLED", "true")

    class _Client:
        async def upload_chat_file(self, file_name, content, media_type):
            return {
                "id": "att-1",
                "name": file_name,
                "mime_type": media_type,
                "created_by": "tenant-1",
            }

        async def understand_file(self, chat_id, file, request_id=None):
            raise RAGFlowAPIError("RAGFlow API request failed", 0)

    pending = [
        PendingAttachment(
            file_name="paste.png",
            media_type="image/png",
            content=b"png",
            sha256="b" * 64,
            size_bytes=3,
        )
    ]
    observations = await observe_attachments(pending, _Client(), chat_id=None)
    assert len(observations) == 1
    assert observations[0].understood is False
    assert observations[0].text_spans == []
