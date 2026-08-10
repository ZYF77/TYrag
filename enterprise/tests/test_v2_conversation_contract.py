"""Runtime contract tests for the frozen v2 conversation API."""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query import formal_router, v2_router, v2_store
from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub
from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping


BASE = "/enterprise/api/v2"


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
    application = FastAPI()
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_user_principal] = _principal
    stub = RAGFlowQueryStub()
    formal_router._query_stub = stub
    value = SimpleNamespace(app=application, db=db, stub=stub)
    try:
        yield value
    finally:
        formal_router._query_stub = None


def _client(runtime) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=runtime.app), base_url="http://test"
    )


async def _create_conversation(client: AsyncClient, **context) -> dict:
    response = await client.post(f"{BASE}/conversations", json=context)
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_document(
    db,
    *,
    external_id: str,
    ragflow_id: str,
    equipment_id: str,
    fixed_asset_no: str,
    dataset_id: str = "ds-v2",
    version_id: str = "v1",
) -> ExtDocumentMap:
    return await insert_mapping(
        db,
        ExtDocumentMap(
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id=external_id,
            source_version_id=version_id,
            event_id=str(uuid.uuid4()),
            sha256=hashlib.sha256(external_id.encode()).hexdigest(),
            file_name=f"{external_id}.pdf",
            asset_id=fixed_asset_no,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
            department_id="d10",
            security_level=2,
            allow_group_ids=json.dumps(["maintenance"]),
            deny_group_ids="[]",
            ragflow_dataset_id=dataset_id,
            ragflow_document_id=ragflow_id,
            sync_status="ready",
            pipeline_status="DONE",
            business_status="active",
            current_version=1,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["systemPrompt", "hiddenPrompt"])
async def test_create_rejects_client_controlled_prompt_fields(runtime, field):
    async with _client(runtime) as client:
        response = await client.post(
            f"{BASE}/conversations", json={field: "override"}
        )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["systemPrompt", "hiddenPrompt"])
async def test_message_rejects_client_controlled_prompt_fields(runtime, field):
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": f"prompt-{field}",
                "question": "question",
                field: "override",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_and_list_use_stable_cursor(runtime):
    async with _client(runtime) as client:
        created = [await _create_conversation(client) for _ in range(3)]
        first = await client.get(f"{BASE}/conversations", params={"limit": 2})
        assert first.status_code == 200
        first_page = first.json()
        assert len(first_page["items"]) == 2
        assert first_page["hasMore"] is True
        assert first_page["nextCursor"]

        second = await client.get(
            f"{BASE}/conversations",
            params={"limit": 2, "cursor": first_page["nextCursor"]},
        )

    assert second.status_code == 200
    second_page = second.json()
    assert len(second_page["items"]) == 1
    seen = {
        item["conversationId"]
        for item in first_page["items"] + second_page["items"]
    }
    assert seen == {item["conversationId"] for item in created}
    assert second_page["hasMore"] is False
    assert second_page["nextCursor"] is None


@pytest.mark.asyncio
async def test_contextless_draft_cannot_send_message(runtime):
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "draft-message", "question": "where?"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "CONVERSATION_CONTEXT_REQUIRED"


@pytest.mark.asyncio
async def test_pending_duplicate_returns_same_run_without_second_user_message(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-PENDING', 'FA-PENDING', 'FA-PENDING')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-PENDING")
        req = v2_router.CreateMessageRequest(
            clientMessageId="pending-1", question="pending"
        )
        run = await v2_store.reserve_message_run(
            runtime.db,
            conversation_id=conversation["conversationId"],
            tenant_id="customer-a",
            business_user_id="biz-user-001",
            client_message_id=req.clientMessageId,
            request_hash=v2_router._request_hash(req),
            run_id="stable-pending-run",
            user_message_id="pending-user-message",
            assistant_message_id="pending-assistant-message",
            question=req.question,
            lease_seconds=3600,
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json=req.model_dump(exclude_none=True),
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}/messages"
        )

    assert run["run_id"] == "stable-pending-run"
    assert response.status_code == 202
    assert response.json()["runId"] == "stable-pending-run"
    assert len([item for item in history.json()["items"] if item["role"] == "user"]) == 1


@pytest.mark.asyncio
async def test_expired_duplicate_is_stable_run_interrupted(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-EXPIRED', 'FA-EXPIRED', 'FA-EXPIRED')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-EXPIRED")
        req = v2_router.CreateMessageRequest(
            clientMessageId="expired-1", question="expired"
        )
        await v2_store.reserve_message_run(
            runtime.db,
            conversation_id=conversation["conversationId"],
            tenant_id="customer-a",
            business_user_id="biz-user-001",
            client_message_id=req.clientMessageId,
            request_hash=v2_router._request_hash(req),
            run_id="stable-expired-run",
            user_message_id="expired-user-message",
            assistant_message_id="expired-assistant-message",
            question=req.question,
            lease_seconds=-1,
        )
        first = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json=req.model_dump(exclude_none=True),
        )
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json=req.model_dump(exclude_none=True),
        )

    assert first.status_code == replay.status_code == 503
    assert first.json()["code"] == replay.json()["code"] == "RUN_INTERRUPTED"


@pytest.mark.asyncio
async def test_context_version_and_alias_conflict(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-A",
        ragflow_id="doc-a",
        equipment_id="EQ-A",
        fixed_asset_no="FA-A",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-B",
        ragflow_id="doc-b",
        equipment_id="EQ-B",
        fixed_asset_no="FA-B",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        conversation_id = conversation["conversationId"]
        assert conversation["contextVersion"] == 0

        updated = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-A"},
        )
        assert updated.status_code == 200
        assert updated.json()["contextVersion"] == 1
        assert updated.json()["context"]["fixedAssetNo"] == "FA-A"

        unchanged = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-A", "fixedAssetNo": "FA-A"},
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["contextVersion"] == 1

        conflict = await client.post(
            f"{BASE}/conversations",
            json={"equipmentId": "EQ-A", "fixedAssetNo": "FA-B"},
        )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONVERSATION_CONTEXT_CONFLICT"


@pytest.mark.asyncio
async def test_equipment_identity_is_immutable_after_first_message(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-IMMUTABLE-A",
        ragflow_id="doc-immutable-a",
        equipment_id="EQ-IMMUTABLE-A",
        fixed_asset_no="FA-IMMUTABLE-A",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-IMMUTABLE-B",
        ragflow_id="doc-immutable-b",
        equipment_id="EQ-IMMUTABLE-B",
        fixed_asset_no="FA-IMMUTABLE-B",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-IMMUTABLE-A"
        )
        message = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "immutable-first", "question": "first"},
        )
        changed = await client.patch(
            f"{BASE}/conversations/{conversation['conversationId']}/context",
            json={"equipmentId": "EQ-IMMUTABLE-B"},
        )

    assert message.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["code"] == "CONVERSATION_CONTEXT_STALE"


@pytest.mark.asyncio
async def test_asset_registry_failure_is_retryable_and_fail_closed(runtime, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ASSET_REGISTRY_MODE", "http")
    async with _client(runtime) as client:
        response = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-UNAVAILABLE"}
        )

    assert response.status_code == 503
    assert response.json()["code"] == "ASSET_REGISTRY_UNAVAILABLE"
    assert response.json()["retryable"] is True


@pytest.mark.asyncio
async def test_context_filters_actual_ragflow_doc_ids(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-A",
        ragflow_id="doc-1",
        equipment_id="EQ-A",
        fixed_asset_no="FA-A",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-B",
        ragflow_id="doc-2",
        equipment_id="EQ-B",
        fixed_asset_no="FA-B",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-A")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "context-filter", "question": "检查故障"},
        )

    assert response.status_code == 200, response.text
    assert runtime.stub._last_completion_body["doc_ids"] == "doc-1"


@pytest.mark.asyncio
async def test_client_message_id_replay_and_conflict(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-TEST', 'FA-TEST', 'FA-TEST')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-TEST")
        url = f"{BASE}/conversations/{conversation['conversationId']}/messages"
        payload = {"clientMessageId": "message-1", "question": "first"}
        first = await client.post(url, json=payload)
        replay = await client.post(url, json=payload)
        conflict = await client.post(
            url,
            json={"clientMessageId": "message-1", "question": "different"},
        )

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["messageId"] == first.json()["messageId"]
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CLIENT_MESSAGE_ID_CONFLICT"


@pytest.mark.asyncio
async def test_message_accept_negotiates_json_and_sse(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-TEST', 'FA-TEST', 'FA-TEST')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-TEST")
        url = f"{BASE}/conversations/{conversation['conversationId']}/messages"
        json_response = await client.post(
            url,
            headers={"Accept": "application/json"},
            json={"clientMessageId": "json-1", "question": "json"},
        )
        sse_response = await client.post(
            url,
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "sse-1", "question": "sse"},
        )

    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    assert sse_response.status_code == 200
    assert sse_response.headers["content-type"].startswith("text/event-stream")
    assert "event: run.started" in sse_response.text
    assert "event: answer.completed" in sse_response.text


@pytest.mark.asyncio
async def test_messages_are_cursor_paginated_without_duplicates(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-TEST', 'FA-TEST', 'FA-TEST')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-TEST")
        conversation_id = conversation["conversationId"]
        url = f"{BASE}/conversations/{conversation_id}/messages"
        for index in range(2):
            response = await client.post(
                url,
                json={
                    "clientMessageId": f"page-{index}",
                    "question": f"question {index}",
                },
            )
            assert response.status_code == 200

        first = await client.get(url, params={"limit": 2})
        first_page = first.json()
        second = await client.get(
            url,
            params={"limit": 2, "cursor": first_page["nextCursor"]},
        )

    assert first.status_code == 200
    assert first_page["hasMore"] is True
    assert len(first_page["items"]) == 2
    assert second.status_code == 200
    assert len(second.json()["items"]) == 2
    first_ids = {item["messageId"] for item in first_page["items"]}
    second_ids = {item["messageId"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_archived_conversation_is_read_only(runtime):
    await runtime.db.execute(
        "INSERT INTO ext_asset_registry (tenant_id, equipment_id, fixed_asset_no, asset_id) VALUES ('customer-a', 'EQ-TEST', 'FA-TEST', 'FA-TEST')"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-TEST")
        conversation_id = conversation["conversationId"]
        messages_url = f"{BASE}/conversations/{conversation_id}/messages"
        before = await client.post(
            messages_url,
            json={"clientMessageId": "before-archive", "question": "before"},
        )
        assert before.status_code == 200
        archived = await client.post(
            f"{BASE}/conversations/{conversation_id}/archive"
        )
        detail = await client.get(f"{BASE}/conversations/{conversation_id}")
        history = await client.get(messages_url)
        suggestions = await client.get(
            f"{BASE}/conversations/{conversation_id}/suggestions"
        )
        context_write = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"faultCode": "E-1"},
        )
        message_write = await client.post(
            messages_url,
            json={"clientMessageId": "after-archive", "question": "after"},
        )

    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert detail.status_code == history.status_code == suggestions.status_code == 200
    assert context_write.status_code == 409
    assert context_write.json()["code"] == "CONVERSATION_ARCHIVED"
    assert message_write.status_code == 409
    assert message_write.json()["code"] == "CONVERSATION_ARCHIVED"


@pytest.mark.asyncio
async def test_suggestion_requires_current_context_version(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-SUGGEST",
        ragflow_id="doc-1",
        equipment_id="EQ-S",
        fixed_asset_no="FA-S",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        conversation_id = conversation["conversationId"]
        suggestion_url = f"{BASE}/conversations/{conversation_id}/suggestions"
        old = (await client.get(suggestion_url)).json()
        assert old["contextVersion"] == 0
        assert old["items"][0]["contextVersion"] == 0

        updated = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-S"},
        )
        assert updated.json()["contextVersion"] == 1
        message_url = f"{BASE}/conversations/{conversation_id}/messages"
        stale = await client.post(
            message_url,
            json={
                "clientMessageId": "stale-suggestion",
                "suggestionId": old["items"][0]["suggestionId"],
                "contextVersion": 0,
            },
        )
        current = (await client.get(suggestion_url)).json()
        accepted = await client.post(
            message_url,
            json={
                "clientMessageId": "current-suggestion",
                "suggestionId": current["items"][0]["suggestionId"],
                "contextVersion": current["contextVersion"],
            },
        )

    assert stale.status_code == 409
    assert stale.json()["code"] == "SUGGESTION_STALE"
    assert current["contextVersion"] == 1
    assert all(item["contextVersion"] == 1 for item in current["items"])
    assert accepted.status_code == 200


class _ExplicitOutcomeStub(RAGFlowQueryStub):
    def __init__(self, *, status: str, include_chunk: bool) -> None:
        super().__init__()
        self.status = status
        self.include_chunk = include_chunk

    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        self._last_completion_body = {
            "chat_id": chat_id,
            "question": question,
            "session_id": session_id,
            "doc_ids": ",".join(doc_ids) if doc_ids else None,
        }
        chunks = []
        if self.include_chunk:
            chunks.append(
                {
                    "id": "internal-chunk-id",
                    "document_id": "doc-1",
                    "document_name": "manual.pdf",
                    "content": "evidence",
                    "positions": [[2, 0.1, 0.2, 0.3, 0.4]],
                }
            )
        return {
            "code": 0,
            "data": {
                "answer": "explicit answer",
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": self.status,
                "reference": {"chunks": chunks},
            },
        }


class _MultiDeltaStub(RAGFlowQueryStub):
    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        yield {"code": 0, "data": {"answer": "first ", "final": False, "session_id": "s"}}
        yield {"code": 0, "data": {"answer": "second", "final": False, "session_id": "s"}}
        yield {"code": 0, "data": {"answer": "", "final": True, "session_id": "s", "reference": {"chunks": []}}}
        yield {"code": 0, "data": True}


@pytest.mark.asyncio
async def test_v2_sse_forwards_upstream_deltas(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-STREAM",
        ragflow_id="doc-stream",
        equipment_id="EQ-STREAM",
        fixed_asset_no="FA-STREAM",
    )
    formal_router._query_stub = _MultiDeltaStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-STREAM")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "multi-delta", "question": "stream"},
        )

    assert response.status_code == 200
    assert response.text.count("event: answer.delta") == 2
    assert '"content": "first "' in response.text
    assert '"content": "second"' in response.text
    assert "first second" not in response.text.split("event: answer.delta", 1)[-1].split(
        "event: answer.completed", 1
    )[0]


@pytest.mark.asyncio
async def test_citation_uses_external_fields_and_state_is_independent(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-1",
        ragflow_id="doc-1",
        equipment_id="EQ-C",
        fixed_asset_no="FA-C",
        version_id="version-external-1",
    )
    stub = _ExplicitOutcomeStub(
        status="no_reliable_evidence", include_chunk=True
    )
    formal_router._query_stub = stub
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-C")
        conversation_id = conversation["conversationId"]
        result = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "citation-state", "question": "question"},
        )
        assert result.status_code == 200, result.text
        body = result.json()
        citation = body["citations"][0]
        citation_response = await client.get(
            f"{BASE}/citations/{citation['citationId']}"
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation_id}/messages"
        )

    assert body["status"] == "no_reliable_evidence"
    assert body["citations"]
    assert citation["externalDocumentId"] == "EXT-DOC-1"
    assert citation["sourceVersionId"] == "version-external-1"
    assert not {
        "documentId",
        "versionId",
        "ragflowDocumentId",
        "chunkId",
    }.intersection(citation)
    assert citation_response.status_code == 200
    assert citation_response.json() == citation
    assistant = [
        item for item in history.json()["items"] if item["role"] == "assistant"
    ][0]
    assert assistant["status"] == "no_reliable_evidence"
    assert assistant["citations"] == [citation]


@pytest.mark.asyncio
async def test_completed_state_does_not_require_citations(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-EMPTY",
        ragflow_id="doc-empty",
        equipment_id="EQ-E",
        fixed_asset_no="FA-E",
    )
    formal_router._query_stub = _ExplicitOutcomeStub(
        status="completed", include_chunk=False
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-E")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "completed-empty", "question": "question"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["citations"] == []
