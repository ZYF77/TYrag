"""Unit tests for RAGFlowClient using stub."""

import pytest
from enterprise.gateway.ragflow_client import (
    RAGFlowClient,
    RAGFlowError,
    RAGFlowStub,
    HealthStatus,
)
from enterprise.gateway.query.ragflow_client import (
    RAGFlowQueryClient,
    _json_missing_payload,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError


class TestRAGFlowStub:
    def test_stub_healthy(self, ragflow_stub):
        status = ragflow_stub.health_check()
        assert status.live is True
        assert status.ready is True
        assert status.request_id is not None

    def test_stub_unhealthy(self, ragflow_stub_unhealthy):
        status = ragflow_stub_unhealthy.health_check()
        assert status.live is False
        assert status.error is not None

    def test_stub_version(self, ragflow_stub):
        v = ragflow_stub.get_version()
        assert "version" in v

    def test_stub_version_unhealthy_raises(self, ragflow_stub_unhealthy):
        with pytest.raises(RAGFlowError):
            ragflow_stub_unhealthy.get_version()


class TestHealthStatus:
    def test_default(self):
        s = HealthStatus(live=False)
        assert s.ready is False

    def test_live_and_ready(self):
        s = HealthStatus(live=True, ready=True, version="v0.26.4", doc_engine="elasticsearch")
        assert s.live
        assert s.ready
        assert s.version == "v0.26.4"


class TestRAGFlowClientTimeout:
    def test_timeout_default(self):
        client = RAGFlowClient()
        assert client.timeout == 120.0

    def test_timeout_custom(self):
        client = RAGFlowClient(timeout=5.0)
        assert client.timeout == 5.0


@pytest.mark.asyncio
async def test_query_start_parsing_uses_canonical_chunks_endpoint():
    client = RAGFlowQueryClient()
    captured = {}

    async def fake_run_sync(fn, *args, **kwargs):
        del fn
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"code": 0, "data": True}

    client._run_sync = fake_run_sync
    result = await client.start_parsing(
        "dataset-1", ["document-1"], request_id="request-1"
    )

    assert captured["args"] == (
        "POST",
        "/api/v1/datasets/dataset-1/chunks",
        "request-1",
    )
    assert captured["kwargs"] == {"json_data": {"document_ids": ["document-1"]}}
    assert result == {"code": 0, "data": True}


@pytest.mark.asyncio
async def test_v2_completion_sends_session_without_projected_messages():
    client = RAGFlowQueryClient()
    captured = {}

    async def fake_run_sync(fn, *args, **kwargs):
        captured.update(kwargs)
        return {"code": 0, "data": {"session_id": "sess-1"}}

    client._run_sync = fake_run_sync
    await client.chat_completion(
        "chat-1",
        "current",
        session_id="sess-1",
        grounding_version=1,
        allowed_identifiers=["EQ-1"],
    )

    body = captured["json_data"]
    assert body["session_id"] == "sess-1"
    assert "messages" not in body
    assert body["question"] == "current"
    assert body["grounding_version"] == 1
    assert body["allowed_identifiers"] == ["EQ-1"]


def test_json_missing_payload_treats_code_102_as_missing():
    payload = b'{"code":102,"message":"The document is not found."}'
    assert _json_missing_payload(payload, "application/json") is True
    assert _json_missing_payload(payload, "text/plain") is True
    assert _json_missing_payload(payload, "image/png") is True
    assert _json_missing_payload(b"\x89PNG\r\n\x1a\n", "image/png") is False
    assert _json_missing_payload(b'{"code":0,"data":"ok"}', "application/json") is False


@pytest.mark.asyncio
async def test_get_document_image_does_not_forward_json_102_as_binary():
    client = RAGFlowQueryClient()

    async def fake_run_sync(fn, *args, **kwargs):
        return (
            b'{"code":102,"message":"The document is not found."}',
            "application/json",
        )

    client._run_sync = fake_run_sync
    assert await client.get_document_image("kb-id-page-1.png") is None


@pytest.mark.asyncio
async def test_get_document_image_returns_none_on_api_error():
    client = RAGFlowQueryClient()

    async def fake_run_sync(fn, *args, **kwargs):
        raise RAGFlowAPIError("missing", 404)

    client._run_sync = fake_run_sync
    assert await client.get_document_image("kb-id-page-1.png") is None
