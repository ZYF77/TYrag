"""Unit tests for chat-attachment upload / understand wire format."""

from __future__ import annotations

import io

import pytest

from enterprise.gateway.query.ragflow_client import RAGFlowQueryClient


@pytest.mark.asyncio
async def test_upload_chat_file_uses_documents_upload_not_file_manager(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None):
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
async def test_understand_file_passes_attachment_descriptor_not_bare_id(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    async def fake_completion(chat_id, question, session_id=None, doc_ids=None, request_id=None, files=None):
        captured["chat_id"] = chat_id
        captured["session_id"] = session_id
        captured["files"] = files
        captured["question"] = question
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

    def fake_sync(method, path, request_id, json_data=None, files=None):
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
    assert captured["json"]["files"][0]["mime_type"] == "image/png"



@pytest.mark.asyncio
async def test_chat_completion_forwards_file_descriptors(monkeypatch):
    client = RAGFlowQueryClient(base_url="http://ragflow.test", api_key="k")
    captured: dict = {}

    def fake_sync(method, path, request_id, json_data=None, files=None):
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
    await client.chat_completion("chat-1", "see image", files=[desc])
    assert captured["path"] == "/api/v1/chat/completions"
    assert captured["json"]["files"] == [desc]
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
        "chat-1", "see image", files=[desc]
    ):
        payloads.append(payload)
    assert captured["json"]["files"] == [desc]
    assert captured["json"]["stream"] is True
    assert payloads and payloads[-1]["data"] is True
