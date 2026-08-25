"""Runtime contract tests for the frozen v2 conversation API."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query import formal_router, v2_router, v2_store
from enterprise.gateway.query.citation_file import set_citation_image_fetcher
from enterprise.gateway.query.ragflow_client import RAGFlowAPIError, RAGFlowQueryStub
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    insert_mapping,
    update_mapping_status,
)


BASE = "/enterprise/api/v2"
_DEFAULT_ASSET_ID = object()


def test_message_one_of_rejects_explicit_null_opposite_fields():
    with pytest.raises(ValueError):
        v2_router.CreateMessageRequest.model_validate(
            {"clientMessageId": "m-1", "question": "q", "suggestionId": None}
        )
    with pytest.raises(ValueError):
        v2_router.CreateMessageRequest.model_validate(
            {
                "clientMessageId": "m-2",
                "suggestionId": "s-1",
                "contextVersion": 1,
                "question": None,
            }
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
    fixed_asset_no: str | None,
    asset_id: str | None | object = _DEFAULT_ASSET_ID,
    dataset_id: str = "ds-v2",
    version_id: str = "v1",
    allow_group_ids: list[str] | None = None,
    **extra,
) -> ExtDocumentMap:
    payload = dict(
        tenant_id="customer-a",
        source_system="DEMO",
        external_document_id=external_id,
        source_version_id=version_id,
        event_id=str(uuid.uuid4()),
        sha256=hashlib.sha256(external_id.encode()).hexdigest(),
        file_name=f"{external_id}.pdf",
        asset_id=fixed_asset_no if asset_id is _DEFAULT_ASSET_ID else asset_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        department_id="d10",
        security_level=2,
        allow_group_ids=json.dumps(allow_group_ids or ["maintenance"]),
        deny_group_ids="[]",
        ragflow_dataset_id=dataset_id,
        ragflow_document_id=ragflow_id,
        sync_status="ready",
        pipeline_status="DONE",
        business_status="active",
        current_version=1,
    )
    payload.update(extra)
    return await insert_mapping(db, ExtDocumentMap(**payload))


def _stub_doc_ids(runtime) -> set[str]:
    raw = (runtime.stub._last_completion_body or {}).get("doc_ids") or ""
    return {item for item in raw.split(",") if item}


async def _configure_web_stub(runtime) -> None:
    await runtime.stub.create_chat(
        "enterprise-formal-customer-a",
        ["ds-v2"],
        prompt_config={
            "web_search_provider": "tavily",
            "tavily_api_key": "test-key",
        },
    )
    runtime.stub._ignore_doc_scope = True
    runtime.stub._omit_default_chunk = True
    runtime.stub._extra_chunks = [
        {
            "id": "web-result-1",
            "document_id": "web-document-1",
            "document_name": "联网结果",
            "content": "厂家发布了最新维护公告。",
            "url": "https://example.com/maintenance",
        }
    ]
    runtime.stub.forced_answer = "厂家维护公告见来源。[ID:0]"


def _citation_identity(item: dict) -> dict:
    return {
        key: value
        for key, value in item.items()
        if key not in {"downloadUrl", "downloadExpiresAt"}
    }


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
async def test_contextless_draft_uses_acl_global_retrieval(runtime):
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
    await _insert_document(
        runtime.db,
        external_id="DOC-DENIED",
        ragflow_id="doc-denied",
        equipment_id="EQ-DENIED",
        fixed_asset_no="FA-DENIED",
        allow_group_ids=["other-team"],
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "draft-message", "question": "离心泵怎么保养"},
        )
        detail = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}"
        )

    assert response.status_code == 200, response.text
    assert detail.json()["equipmentId"] is None
    assert _stub_doc_ids(runtime) == {"doc-1", "doc-2", "doc-denied"}
    question = runtime.stub._last_completion_body["question"]
    # Unbound drafts must not prepend Gateway identity text into the retrieval
    # query; equipment hint stays on the answer via _with_equipment_hint.
    assert question == "离心泵怎么保养"
    assert not question.startswith(v2_router.GLOBAL_QUESTION_PREFIX)
    assert response.json()["answer"].endswith(v2_router.EQUIPMENT_ID_HINT)


@pytest.mark.asyncio
async def test_first_message_binds_unique_ingested_equipment(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GD",
        ragflow_id="doc-gd",
        equipment_id="GD01250002",
        fixed_asset_no="FA-GD",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-OTHER",
        ragflow_id="doc-other",
        equipment_id="EQ-OTHER",
        fixed_asset_no="FA-OTHER",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "bind-unique",
                "question": "帮我查 GD01250002 的说明书",
            },
        )
        detail = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}"
        )

    assert response.status_code == 200, response.text
    assert detail.json()["equipmentId"] == "GD01250002"
    assert detail.json()["fixedAssetNo"] == "FA-GD"
    assert _stub_doc_ids(runtime) == {"doc-gd"}
    assert runtime.stub._last_completion_body["question"] == "帮我查 GD01250002 的说明书"
    assert v2_router.EQUIPMENT_ID_HINT not in response.json()["answer"]


@pytest.mark.asyncio
async def test_explicit_compare_and_unknown_equipment_use_turn_scope(runtime):
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
        conversation = await _create_conversation(client)
        compared = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "compare-two",
                "question": "对比 EQ-A 和 EQ-B 的保养要求",
            },
        )
        compare_ids = _stub_doc_ids(runtime)
        unknown = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "unknown",
                "question": "查一下 EQ-MISSING 的图纸",
            },
        )
        detail = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}"
        )

    assert compared.status_code == unknown.status_code == 200
    assert compare_ids == {"doc-1", "doc-2"}
    assert detail.json()["equipmentId"] == "EQ-B"
    assert unknown.json()["status"] == "无可靠依据"
    async with runtime.db.execute(
        """SELECT entity_scope_json, allowed_doc_ids_json
             FROM ext_v2_message_run
            WHERE client_message_id='unknown'"""
    ) as cursor:
        snapshot = await cursor.fetchone()
    assert json.loads(snapshot["entity_scope_json"]) == []
    assert json.loads(snapshot["allowed_doc_ids_json"]) == []


@pytest.mark.asyncio
async def test_second_message_can_bind_after_global_search(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GD",
        ragflow_id="doc-gd",
        equipment_id="GD01250002",
        fixed_asset_no="FA-GD",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-OTHER",
        ragflow_id="doc-other",
        equipment_id="EQ-OTHER",
        fixed_asset_no="FA-OTHER",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        first = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "global-first", "question": "这类设备怎么保养"},
        )
        second = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "bind-second",
                "question": "帮我查 GD01250002 的说明书",
            },
        )
        detail = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}"
        )

    assert first.status_code == second.status_code == 200
    assert detail.json()["equipmentId"] == "GD01250002"
    assert _stub_doc_ids(runtime) == {"doc-gd"}
    assert v2_router.EQUIPMENT_ID_HINT not in second.json()["answer"]


@pytest.mark.asyncio
async def test_patch_can_bind_equipment_after_unbound_first_message(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-A",
        ragflow_id="doc-1",
        equipment_id="EQ-A",
        fixed_asset_no="FA-A",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        first = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "unbound-first", "question": "先随便问一下"},
        )
        patched = await client.patch(
            f"{BASE}/conversations/{conversation['conversationId']}/context",
            json={"equipmentId": "EQ-A"},
        )

    assert first.status_code == 200
    assert patched.status_code == 200, patched.text
    assert patched.json()["equipmentId"] == "EQ-A"


@pytest.mark.asyncio
async def test_role_acl_is_open_within_tenant_during_test_stage(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-DENIED",
        ragflow_id="doc-denied",
        equipment_id="EQ-DENIED",
        fixed_asset_no="FA-DENIED",
        allow_group_ids=["other-team"],
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "empty-acl", "question": "随便问"},
        )

    assert response.status_code == 200
    assert _stub_doc_ids(runtime) == {"doc-denied"}


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
async def test_context_version_and_eam_fields_are_persisted_as_submitted(runtime):
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
        assert updated.json()["context"]["equipmentId"] == "EQ-A"
        assert updated.json()["context"]["fixedAssetNo"] is None

        with_fixed = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-A", "fixedAssetNo": "FA-A"},
        )
        assert with_fixed.status_code == 200
        assert with_fixed.json()["contextVersion"] == 2
        assert with_fixed.json()["context"]["fixedAssetNo"] == "FA-A"

        unchanged = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-A", "fixedAssetNo": "FA-A"},
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["contextVersion"] == 2

        # Trust EAM: mismatched pair is accepted and stored as submitted.
        accepted = await client.post(
            f"{BASE}/conversations",
            json={"equipmentId": "EQ-A", "fixedAssetNo": "FA-B"},
        )

    assert accepted.status_code == 201
    assert accepted.json()["equipmentId"] == "EQ-A"
    assert accepted.json()["fixedAssetNo"] == "FA-B"
    assert accepted.json()["context"]["registryVersion"] is None


@pytest.mark.asyncio
async def test_equipment_context_can_switch_after_first_message(runtime):
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
        after_switch = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "after-patch-switch", "question": "它呢？"},
        )

    assert message.status_code == 200
    assert changed.status_code == 200
    assert changed.json()["equipmentId"] == "EQ-IMMUTABLE-B"
    assert after_switch.status_code == 200
    assert _stub_doc_ids(runtime) == {"doc-immutable-b"}


@pytest.mark.asyncio
async def test_turn_scope_switch_compare_and_snapshot(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-TURN-A",
        ragflow_id="doc-turn-a",
        equipment_id="EQ-A",
        fixed_asset_no="FA-A",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-TURN-B",
        ragflow_id="doc-turn-b",
        equipment_id="EQ-B",
        fixed_asset_no="FA-B",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-A")
        conversation_id = conversation["conversationId"]
        first = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "turn-a", "question": "它怎么维护？"},
        )
        second = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "turn-b", "question": "那 EQ-B 呢？"},
        )
        compared = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={
                "clientMessageId": "turn-compare",
                "question": "和刚才那台比有什么区别？",
            },
        )
        previous_compare_ids = _stub_doc_ids(runtime)
        explicit_compare = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={
                "clientMessageId": "turn-explicit-compare",
                "question": "直接比较 EQ-A 和 EQ-B",
            },
        )
        detail = await client.get(f"{BASE}/conversations/{conversation_id}")

    assert (
        first.status_code
        == second.status_code
        == compared.status_code
        == explicit_compare.status_code
        == 200
    )
    assert detail.json()["equipmentId"] == "EQ-B"
    assert previous_compare_ids == {"doc-turn-a", "doc-turn-b"}
    assert _stub_doc_ids(runtime) == {"doc-turn-a", "doc-turn-b"}
    async with runtime.db.execute(
        """SELECT entity_scope_json, allowed_doc_ids_json
             FROM ext_v2_message_run
            WHERE client_message_id='turn-explicit-compare'"""
    ) as cursor:
        snapshot = await cursor.fetchone()
    assert set(json.loads(snapshot["entity_scope_json"])) == {"EQ-A", "EQ-B"}
    assert set(json.loads(snapshot["allowed_doc_ids_json"])) == {
        "doc-turn-a",
        "doc-turn-b",
    }


@pytest.mark.asyncio
async def test_unknown_explicit_equipment_fails_closed(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-KNOWN",
        ragflow_id="doc-known",
        equipment_id="EQ-KNOWN",
        fixed_asset_no="FA-KNOWN",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-KNOWN")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "unknown-equipment",
                "question": "请查询 EQ-UNKNOWN 的资料",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "无可靠依据"
    async with runtime.db.execute(
        """SELECT allowed_doc_ids_json FROM ext_v2_message_run
            WHERE client_message_id='unknown-equipment'"""
    ) as cursor:
        snapshot = await cursor.fetchone()
    assert json.loads(snapshot["allowed_doc_ids_json"]) == []


@pytest.mark.asyncio
async def test_create_with_equipment_ignores_unconfigured_asset_registry(
    runtime, monkeypatch
):
    monkeypatch.setenv("ENTERPRISE_EAM_ASSET_RESOLVER_MODE", "http")
    monkeypatch.delenv("ENTERPRISE_EAM_ASSET_RESOLVER_BASE_URL", raising=False)
    async with _client(runtime) as client:
        response = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-UNAVAILABLE"}
        )

    assert response.status_code == 201
    body = response.json()
    assert body["equipmentId"] == "EQ-UNAVAILABLE"
    assert body["fixedAssetNo"] is None
    assert body["context"]["registryVersion"] is None
    assert body["suggestions"]
    assert body["contextCompacted"] is False


@pytest.mark.asyncio
async def test_eam_context_snapshot_persists_submitted_identity(runtime):
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client,
            equipmentId="EQ-SNAPSHOT",
            fixedAssetNo="FA-SNAPSHOT",
        )

    async with runtime.db.execute(
        "SELECT equipment_id, fixed_asset_no, asset_id, registry_version, "
        "context_resolved_at FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    ) as cursor:
        snapshot = await cursor.fetchone()

    assert snapshot["equipment_id"] == "EQ-SNAPSHOT"
    assert snapshot["fixed_asset_no"] == "FA-SNAPSHOT"
    assert snapshot["asset_id"] is None
    assert snapshot["registry_version"] is None
    assert snapshot["context_resolved_at"]


@pytest.mark.asyncio
async def test_context_version_write_conflict_is_stable_409(runtime, monkeypatch):
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)

    stale_write = await v2_store.update_context(
        runtime.db,
        conversation_id=conversation["conversationId"],
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        equipment_id=None,
        fixed_asset_no=None,
        fault_code="E-STale",
        context_version=1,
        expected_context_version=99,
    )
    assert stale_write is None

    async def conflict(*args, **kwargs):
        return None

    monkeypatch.setattr(v2_store, "update_context", conflict)
    async with _client(runtime) as client:
        response = await client.patch(
            f"{BASE}/conversations/{conversation['conversationId']}/context",
            json={"faultCode": "E-CONFLICT"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "CONVERSATION_CONTEXT_CONFLICT"


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
    assert v2_router.EQUIPMENT_ID_HINT not in response.json()["answer"]


@pytest.mark.asyncio
async def test_context_scope_does_not_match_equipment_alias_fields(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-CANONICAL",
        ragflow_id="doc-canonical",
        equipment_id="EQ-CANONICAL",
        fixed_asset_no="FA-CANONICAL",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-ALIAS-MISMATCH",
        ragflow_id="doc-alias-mismatch",
        equipment_id="EQ-OTHER",
        fixed_asset_no="EQ-CANONICAL",
    )
    # Keep the document mapping to exercise retrieval filtering, but do not
    # let the deliberately inconsistent metadata become a registry fixture.
    await runtime.db.execute(
        "DELETE FROM ext_asset_registry WHERE equipment_id='EQ-OTHER'"
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-CANONICAL"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "canonical-scope", "question": "检查"},
        )

    assert response.status_code == 200
    assert runtime.stub._last_completion_body["doc_ids"] == "doc-canonical"


@pytest.mark.asyncio
async def test_context_scope_uses_submitted_eam_fields_only(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-MISSING-FIXED",
        ragflow_id="doc-missing-fixed",
        equipment_id="EQ-STRICT",
        fixed_asset_no=None,
        asset_id="ASSET-STRICT",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-MATCHING-FIXED",
        ragflow_id="doc-matching-fixed",
        equipment_id="EQ-STRICT",
        fixed_asset_no="FA-STRICT",
        asset_id=None,
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client,
            equipmentId="EQ-STRICT",
            fixedAssetNo="FA-STRICT",
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "strict-context", "question": "检查"},
        )

    assert response.status_code == 200
    # Conversation has no assetId; docs matching equipment+fixed are in scope.
    assert runtime.stub._last_completion_body["doc_ids"] == "doc-matching-fixed"
    assert response.json()["status"] in {"已完成", "无可靠依据"}


@pytest.mark.asyncio
async def test_failed_quality_evaluation_is_not_sent_to_ragflow(runtime):
    document = await _insert_document(
        runtime.db,
        external_id="DOC-QUALITY-FAILED",
        ragflow_id="doc-quality-failed",
        equipment_id="EQ-QUALITY-FAILED",
        fixed_asset_no="FA-QUALITY-FAILED",
    )
    now = datetime.now(timezone.utc).isoformat()
    await runtime.db.execute(
        """INSERT INTO parse_quality_evaluation
           (tenant_id, source_system, external_document_id, source_version_id,
            evaluation_state, parse_quality_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'completed', 'failed', ?, ?)""",
        (
            document.tenant_id,
            document.source_system,
            document.external_document_id,
            document.source_version_id,
            now,
            now,
        ),
    )
    await runtime.db.commit()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-QUALITY-FAILED"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "quality-filter", "question": "检查"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "无可靠依据"
    assert runtime.stub._last_completion_body is None


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
    assert archived.json()["status"] == "已归档"
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


def _grounding(version: int | None, knowledge: str = "evidence") -> dict | None:
    return (
        {"version": version, "effectiveKnowledge": knowledge}
        if version is not None
        else None
    )


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
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ) -> dict:
        del messages, store_history_messages, pass_all_history_messages, kwargs
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
        answer = "explicit answer [ID:0]" if self.include_chunk else "explicit answer"
        return {
            "code": 0,
            "data": {
                "answer": answer,
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": self.status,
                "reference": {"chunks": chunks},
                "grounding": _grounding(grounding_version),
            },
        }


class _SelectiveCitationStub(RAGFlowQueryStub):
    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ) -> dict:
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        return {
            "code": 0,
            "data": {
                "answer": "漏气维修见工单。[ID:1]",
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": "completed",
                "reference": {
                    "chunks": [
                        {
                            "id": "invoice-chunk",
                            "document_id": "doc-1",
                            "document_name": "invoice.pdf",
                            "content": "Cursor Pro invoice",
                        },
                        {
                            "id": "repair-chunk",
                            "document_id": "doc-1",
                            "document_name": "repair.pdf",
                            "content": "leak repair work order",
                        },
                    ],
                },
                "grounding": _grounding(grounding_version),
            },
        }


class _AbstainContrastStub(RAGFlowQueryStub):
    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ) -> dict:
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        return {
            "code": 0,
            "data": {
                "answer": (
                    "当前检索到的知识库中，仅包含调试记录和合格证[ID:0][ID:1]，"
                    "暂无专门的设备维修记录。"
                ),
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": "completed",
                "reference": {
                    "chunks": [
                        {
                            "id": "cert-chunk",
                            "document_id": "doc-1",
                            "document_name": "certificate.pdf",
                            "content": "product certificate",
                        },
                        {
                            "id": "debug-chunk",
                            "document_id": "doc-1",
                            "document_name": "debug.pdf",
                            "content": "commissioning record",
                        },
                    ],
                },
                "grounding": _grounding(grounding_version),
            },
        }


class _ThinkAnswerStub(RAGFlowQueryStub):
    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ) -> dict:
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        return {
            "code": 0,
            "data": {
                "answer": "<think>规划并引用发票 [ID:0]</think>漏气维修见工单。[ID:1]",
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": "completed",
                "reference": {
                    "chunks": [
                        {
                            "id": "invoice-chunk",
                            "document_id": "doc-1",
                            "document_name": "invoice.pdf",
                            "content": "Cursor Pro invoice",
                        },
                        {
                            "id": "repair-chunk",
                            "document_id": "doc-1",
                            "document_name": "repair.pdf",
                            "content": "leak repair work order",
                        },
                    ],
                },
                "grounding": _grounding(grounding_version),
            },
        }


class _ThinkStreamStub(RAGFlowQueryStub):
    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        yield {
            "code": 0,
            "data": {
                "answer": "",
                "start_to_think": True,
                "final": False,
                "session_id": "s",
            },
        }
        yield {"code": 0, "data": {"answer": "规划过程", "final": False, "session_id": "s"}}
        yield {
            "code": 0,
            "data": {"answer": "", "end_to_think": True, "final": False, "session_id": "s"},
        }
        yield {"code": 0, "data": {"answer": "你好呀", "final": False, "session_id": "s"}}
        yield {
            "code": 0,
            "data": {
                "answer": "",
                "final": True,
                "session_id": "s",
                "reference": {"chunks": []},
                "grounding": _grounding(grounding_version),
            },
        }
        yield {"code": 0, "data": True}


class _CropCitationStub(RAGFlowQueryStub):
    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ) -> dict:
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        return {
            "code": 0,
            "data": {
                "answer": "见图示。[ID:0]",
                "id": "ragflow-message",
                "session_id": "ragflow-session",
                "status": "completed",
                "reference": {
                    "chunks": [
                        {
                            "id": "crop-chunk",
                            "document_id": "doc-1",
                            "document_name": "panel.png",
                            "content": "fault panel",
                            "image_id": "ds-v2-page-1.png",
                        }
                    ],
                },
                "grounding": _grounding(grounding_version),
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
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        yield {"code": 0, "data": {"answer": "first ", "final": False, "session_id": "s"}}
        yield {"code": 0, "data": {"answer": "second", "final": False, "session_id": "s"}}
        yield {"code": 0, "data": {"answer": "", "final": True, "session_id": "s", "reference": {"chunks": []}, "grounding": _grounding(grounding_version)}}
        yield {"code": 0, "data": True}


class _TransportFailureStub(RAGFlowQueryStub):
    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, grounding_version, kwargs
        if False:
            yield {}
        raise RAGFlowAPIError("stream transport failed", 0)


class _ScopeViolationAfterDeltaStub(RAGFlowQueryStub):
    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        yield {
            "code": 0,
            "data": {
                "answer": "脏答案引用越权设备。[ID:0]",
                "final": False,
                "session_id": "s",
            },
        }
        yield {
            "code": 0,
            "data": {
                "answer": "",
                "final": True,
                "session_id": "s",
                "status": "completed",
                "reference": {
                    "chunks": [
                        {
                            "id": "leak-chunk",
                            "document_id": "outside-scope-doc",
                            "content": "越权内容",
                        }
                    ]
                },
                "grounding": _grounding(grounding_version),
            },
        }
        yield {"code": 0, "data": True}


class _ExplicitStreamOutcomeStub(RAGFlowQueryStub):
    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        messages: list[dict] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        **kwargs,
    ):
        del chat_id, question, session_id, doc_ids, request_id
        del messages, store_history_messages, pass_all_history_messages, kwargs
        yield {
            "code": 0,
            "data": {
                "answer": "explicit stream answer [ID:0]",
                "status": "no_reliable_evidence",
                "final": False,
            },
        }
        yield {
            "code": 0,
            "data": {
                "answer": "",
                "status": "no_reliable_evidence",
                "final": True,
                "grounding": _grounding(grounding_version),
                "reference": {
                    "chunks": [
                        {
                            "id": "stream-chunk",
                            "document_id": "doc-stream-state",
                            "content": "evidence",
                        }
                    ]
                },
            },
        }
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
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "multi-delta", "question": "stream"},
        )
        json_replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "application/json"},
            json={"clientMessageId": "multi-delta", "question": "stream"},
        )

    assert response.status_code == 200
    assert response.text.count("event: answer.delta") == 2
    assert '"content": "first "' in response.text
    assert '"content": "second"' in response.text
    assert "event: answer.replaced" not in response.text
    assert "event: reasoning.delta" not in response.text
    assert replay.status_code == 200
    assert replay.text.count("event: answer.delta") == 1
    assert '"content": "first second"' in replay.text
    assert "event: answer.replaced" not in replay.text
    assert json_replay.status_code == 200
    assert "_streamDeltas" not in json_replay.json()
    assert json_replay.json()["reasoning"] is None


@pytest.mark.asyncio
async def test_v2_sse_transport_failure_is_stable_and_replayable(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-STREAM-FAIL",
        ragflow_id="doc-stream-fail",
        equipment_id="EQ-STREAM-FAIL",
        fixed_asset_no="FA-STREAM-FAIL",
    )
    formal_router._query_stub = _TransportFailureStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-STREAM-FAIL"
        )
        url = f"{BASE}/conversations/{conversation['conversationId']}/messages"
        response = await client.post(
            url,
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "stream-failure", "question": "stream"},
        )
        replay = await client.post(
            url,
            headers={"Accept": "application/json"},
            json={"clientMessageId": "stream-failure", "question": "stream"},
        )

    assert response.status_code == 200
    assert "event: run.failed" in response.text
    assert '"code": "RAGFLOW_UNAVAILABLE"' in response.text
    assert "event: answer.replaced" not in response.text
    assert replay.status_code == 503
    assert replay.json()["code"] == "RAGFLOW_UNAVAILABLE"


@pytest.mark.asyncio
async def test_v2_sse_scope_violation_clears_streamed_answer(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-SCOPE-SSE",
        ragflow_id="doc-scope-sse",
        equipment_id="EQ-SCOPE-SSE",
        fixed_asset_no="FA-SCOPE-SSE",
    )
    formal_router._query_stub = _ScopeViolationAfterDeltaStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-SCOPE-SSE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "scope-sse", "question": "stream"},
        )

    assert response.status_code == 200
    assert "event: answer.delta" in response.text
    assert "event: answer.replaced" in response.text
    assert '"content": ""' in response.text
    assert "event: run.failed" in response.text
    assert '"code": "RAGFLOW_SCOPE_VIOLATION"' in response.text
    replaced_at = response.text.index("event: answer.replaced")
    failed_at = response.text.index("event: run.failed")
    assert replaced_at < failed_at


@pytest.mark.asyncio
async def test_v2_stream_keeps_business_state_independent_of_citations(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-STREAM-STATE",
        ragflow_id="doc-stream-state",
        equipment_id="EQ-STREAM-STATE",
        fixed_asset_no="FA-STREAM-STATE",
    )
    formal_router._query_stub = _ExplicitStreamOutcomeStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-STREAM-STATE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "stream-state", "question": "stream"},
        )

    assert response.status_code == 200
    assert '"status": "无可靠依据"' in response.text
    assert "event: citation" in response.text


@pytest.mark.asyncio
async def test_message_level_internet_returns_replayable_web_citation(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WEB",
        ragflow_id="doc-web",
        equipment_id="EQ-WEB",
        fixed_asset_no="FA-WEB",
    )
    await _configure_web_stub(runtime)
    payload = {
        "clientMessageId": "web-json",
        "question": "查询厂家最新维护公告",
        "internetEnabled": True,
    }
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-WEB")
        url = f"{BASE}/conversations/{conversation['conversationId']}/messages"
        response = await client.post(url, json=payload)
        replay = await client.post(url, json=payload)
        conflict = await client.post(
            url,
            json={
                "clientMessageId": "web-json",
                "question": "查询厂家最新维护公告",
                "internetEnabled": False,
            },
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}/messages"
        )
        citation = await client.get(
            f"{BASE}/citations/{response.json()['citations'][0]['citationId']}"
        )

    assert response.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CLIENT_MESSAGE_ID_CONFLICT"
    assert runtime.stub._last_completion_body["internet"] is True
    web = response.json()["citations"][0]
    assert web["sourceType"] == "web"
    assert web["url"] == "https://example.com/maintenance"
    assert web["downloadUrl"] is None
    assert web["downloadExpiresAt"] is None
    assert citation.status_code == 200
    assert citation.json() == web
    assistant = next(
        item for item in history.json()["items"] if item["role"] == "assistant"
    )
    assert assistant["citations"] == [web]


@pytest.mark.asyncio
async def test_message_level_internet_streams_web_citation(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WEB-SSE",
        ragflow_id="doc-web-sse",
        equipment_id="EQ-WEB-SSE",
        fixed_asset_no="FA-WEB-SSE",
    )
    await _configure_web_stub(runtime)
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-WEB-SSE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={
                "clientMessageId": "web-sse",
                "question": "联网查询",
                "internetEnabled": True,
            },
        )

    assert response.status_code == 200
    assert "event: citation" in response.text
    assert '"sourceType": "web"' in response.text
    assert '"url": "https://example.com/maintenance"' in response.text
    assert runtime.stub._last_completion_body["internet"] is True


@pytest.mark.asyncio
async def test_internet_without_configured_provider_falls_back_to_internal(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WEB-OFF",
        ragflow_id="doc-web-off",
        equipment_id="EQ-WEB-OFF",
        fixed_asset_no="FA-WEB-OFF",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-WEB-OFF"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "web-off",
                "question": "联网查询",
                "internetEnabled": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "已完成"
    assert runtime.stub._last_completion_body["internet"] is False
    assert runtime.stub._sessions


@pytest.mark.asyncio
async def test_web_url_does_not_bypass_scope_without_internet(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WEB-SCOPE",
        ragflow_id="doc-web-scope",
        equipment_id="EQ-WEB-SCOPE",
        fixed_asset_no="FA-WEB-SCOPE",
    )
    runtime.stub._ignore_doc_scope = True
    runtime.stub._omit_default_chunk = True
    runtime.stub._extra_chunks = [
        {
            "id": "untrusted-url",
            "document_id": "outside-scope",
            "content": "越权内容",
            "url": "https://example.com/outside",
        }
    ]
    runtime.stub.forced_answer = "越权内容。[ID:0]"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-WEB-SCOPE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "web-scope", "question": "普通查询"},
        )

    assert response.status_code == 502
    assert response.json()["code"] == "RAGFLOW_SCOPE_VIOLATION"


@pytest.mark.asyncio
async def test_citation_uses_external_fields_and_state_is_independent(runtime):
    document = await _insert_document(
        runtime.db,
        external_id="EXT-DOC-1",
        ragflow_id="doc-1",
        equipment_id="EQ-C",
        fixed_asset_no="FA-C",
        version_id="version-external-1",
    )
    stub = _ExplicitOutcomeStub(
        status="completed", include_chunk=True
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
        await update_mapping_status(
            runtime.db,
            document,
            "superseded",
            business_status="superseded",
            current_version=0,
        )
        citation_response = await client.get(
            f"{BASE}/citations/{citation['citationId']}"
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation_id}/messages"
        )

    assert body["status"] == "已完成"
    assert body["citations"]
    assert citation["externalDocumentId"] == "EXT-DOC-1"
    assert citation["sourceVersionId"] == "version-external-1"
    assert not {
        "documentId",
        "versionId",
        "ragflowDocumentId",
        "chunkId",
        "imageId",
    }.intersection(citation)
    assert citation["downloadUrl"]
    assert citation["downloadExpiresAt"]
    assert "/file/" in citation["downloadUrl"]
    assert citation_response.status_code == 200
    assert _citation_identity(citation_response.json()) == _citation_identity(citation)
    assistant = [
        item for item in history.json()["items"] if item["role"] == "assistant"
    ][0]
    assert assistant["status"] == "已完成"
    assert [_citation_identity(item) for item in assistant["citations"]] == [
        _citation_identity(citation)
    ]


@pytest.mark.asyncio
async def test_v2_keeps_only_chunks_cited_in_the_answer(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-CITE",
        ragflow_id="doc-1",
        equipment_id="EQ-CITE",
        fixed_asset_no="FA-CITE",
    )
    formal_router._query_stub = _SelectiveCitationStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-CITE")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "cite-filter", "question": "有漏气维修记录吗"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "已完成"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["title"] == "repair.pdf"
    assert "invoice.pdf" not in {item["title"] for item in body["citations"]}


@pytest.mark.asyncio
async def test_v2_keeps_citations_when_no_reliable_evidence(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-NONE",
        ragflow_id="doc-1",
        equipment_id="EQ-NONE",
        fixed_asset_no="FA-NONE",
    )
    formal_router._query_stub = _ExplicitOutcomeStub(
        status="no_reliable_evidence", include_chunk=True
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-NONE")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "no-evidence-cite", "question": "有漏气维修记录吗"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "无可靠依据"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["externalDocumentId"] == "EXT-DOC-NONE"


@pytest.mark.asyncio
async def test_v2_abstain_phrase_keeps_state_and_citations_independent(
    runtime,
):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-ABSTAIN",
        ragflow_id="doc-1",
        equipment_id="EQ-ABSTAIN",
        fixed_asset_no="FA-ABSTAIN",
    )
    formal_router._query_stub = _AbstainContrastStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-ABSTAIN")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "abstain-cite", "question": "设备维修记录有么？"},
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}/messages"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "无可靠依据"
    assert len(body["citations"]) == 2
    assistant = next(
        item for item in history.json()["items"] if item["role"] == "assistant"
    )
    assert assistant["status"] == "无可靠依据"
    assert len(assistant["citations"]) == 2


@pytest.mark.asyncio
async def test_v2_splits_think_from_answer_and_filters_citations_on_body(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-THINK",
        ragflow_id="doc-1",
        equipment_id="EQ-THINK",
        fixed_asset_no="FA-THINK",
    )
    formal_router._query_stub = _ThinkAnswerStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-THINK")
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "think-split", "question": "有漏气维修记录吗"},
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}/messages"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "漏气维修见工单。[ID:1]"
    assert "规划" not in body["answer"]
    assert body["reasoning"] == "规划并引用发票 [ID:0]"
    assert [item["title"] for item in body["citations"]] == ["repair.pdf"]
    items = history.json()["items"]
    user = next(item for item in items if item["role"] == "user")
    assistant = next(item for item in items if item["role"] == "assistant")
    assert user["reasoning"] is None
    assert assistant["content"] == "漏气维修见工单。[ID:1]"
    assert assistant["reasoning"] == "规划并引用发票 [ID:0]"


@pytest.mark.asyncio
async def test_v2_sse_routes_think_tokens_to_reasoning_delta(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-THINK-STREAM",
        ragflow_id="doc-think-stream",
        equipment_id="EQ-THINK-STREAM",
        fixed_asset_no="FA-THINK-STREAM",
    )
    formal_router._query_stub = _ThinkStreamStub()
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-THINK-STREAM"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "think-stream", "question": "你好"},
        )
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "think-stream", "question": "你好"},
        )
        json_replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "application/json"},
            json={"clientMessageId": "think-stream", "question": "你好"},
        )

    assert response.status_code == 200, response.text
    assert "event: reasoning.delta" in response.text
    assert '"content": "规划过程"' in response.text
    assert response.text.count("event: answer.delta") == 1
    assert '"content": "你好呀"' in response.text
    answer_block = response.text.split("event: answer.delta", 1)[-1].split(
        "event: answer.completed", 1
    )[0]
    assert "规划过程" not in answer_block
    assert replay.status_code == 200
    assert "event: reasoning.delta" in replay.text
    assert json_replay.status_code == 200
    assert json_replay.json()["answer"] == "你好呀"
    assert json_replay.json()["reasoning"] == "规划过程"


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"crop-bytes"


@pytest.mark.asyncio
async def test_citation_download_url_serves_crop_without_jwt(runtime):
    await _insert_document(
        runtime.db,
        external_id="EXT-DOC-CROP",
        ragflow_id="doc-1",
        equipment_id="EQ-CROP",
        fixed_asset_no="FA-CROP",
    )
    formal_router._query_stub = _CropCitationStub()

    async def _fetch(image_id: str):
        assert image_id == "ds-v2-page-1.png"
        return PNG_BYTES, "image/png"

    set_citation_image_fetcher(_fetch)
    try:
        async with _client(runtime) as client:
            conversation = await _create_conversation(client, equipmentId="EQ-CROP")
            asked = await client.post(
                f"{BASE}/conversations/{conversation['conversationId']}/messages",
                json={"clientMessageId": "crop-cite", "question": "图上是什么"},
            )
            assert asked.status_code == 200, asked.text
            download_url = asked.json()["citations"][0]["downloadUrl"]
            downloaded = await client.get(download_url)
            replayed = await client.get(download_url)
            missing = await client.get(
                f"{BASE}/citations/cite-missing/file/not-a-ticket"
            )
    finally:
        set_citation_image_fetcher(None)

    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("image/png")
    assert downloaded.content == PNG_BYTES
    assert replayed.status_code == 200
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_citation_download_url_serves_original_without_jwt(
    runtime, tmp_path, monkeypatch
):
    source_path = tmp_path / "original.pdf"
    source_path.write_bytes(b"%PDF-1.4 original-bytes")
    monkeypatch.setenv(
        "ENTERPRISE_FILE_SHARE_ROOTS",
        json.dumps({"test-root": str(tmp_path)}),
    )
    from enterprise.gateway.sync.external_source import FileShareSourceAdapter

    stat = FileShareSourceAdapter().stat_source("test-root", source_path.name)
    document = await _insert_document(
        runtime.db,
        external_id="EXT-DOC-1",
        ragflow_id="doc-1",
        equipment_id="EQ-ORIG",
        fixed_asset_no="FA-ORIG",
    )
    formal_router._query_stub = _ExplicitOutcomeStub(
        status="completed", include_chunk=True
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-ORIG")
        asked = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "orig-cite", "question": "question"},
        )
        assert asked.status_code == 200, asked.text
        download_url = asked.json()["citations"][0]["downloadUrl"]
        await runtime.db.execute(
            """UPDATE ext_document_map
               SET source_kind='FILE_SHARE', storage_root_id=?, relative_path=?,
                   source_size=?, source_modified_ns=?, source_etag=?,
                   file_name=?
               WHERE id=?""",
            (
                "test-root",
                source_path.name,
                stat.size,
                stat.modified_ns,
                stat.etag,
                "original.pdf",
                document.id,
            ),
        )
        await runtime.db.commit()
        downloaded = await client.get(download_url)

    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert downloaded.content == source_path.read_bytes()


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
    assert response.json()["status"] == "已完成"
    assert response.json()["citations"] == []


@pytest.mark.asyncio
async def test_role_group_change_does_not_hide_test_stage_citation(runtime):
    document = await _insert_document(
        runtime.db,
        external_id="EXT-DOC-ACL",
        ragflow_id="doc-1",
        equipment_id="EQ-ACL",
        fixed_asset_no="FA-ACL",
    )
    formal_router._query_stub = _ExplicitOutcomeStub(
        status="completed", include_chunk=True
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-ACL")
        result = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "acl-history", "question": "question"},
        )
        citation_id = result.json()["citations"][0]["citationId"]
        await update_mapping_status(
            runtime.db,
            document,
            "ready",
            allow_group_ids=json.dumps(["revoked-group"]),
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation['conversationId']}/messages"
        )
        citation = await client.get(f"{BASE}/citations/{citation_id}")

    assistant = [item for item in history.json()["items"] if item["role"] == "assistant"][0]
    assert assistant["citations"]
    assert citation.status_code == 200


@pytest.mark.asyncio
async def test_ask_does_not_refresh_context_via_asset_registry(runtime, monkeypatch):
    await _insert_document(
        runtime.db,
        external_id="DOC-TTL",
        ragflow_id="doc-ttl",
        equipment_id="EQ-TTL",
        fixed_asset_no="FA-OLD",
    )
    calls = {"resolve": 0}

    async def boom(*args, **kwargs):
        calls["resolve"] += 1
        raise AssertionError("inquiry must not call resolve_asset")

    monkeypatch.setattr(
        "enterprise.gateway.asset_registry.resolve_asset", boom
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-TTL", fixedAssetNo="FA-OLD"
        )
        conversation_id = conversation["conversationId"]
        await runtime.db.execute(
            "UPDATE ext_v2_conversation SET context_resolved_at=? "
            "WHERE conversation_id=?",
            ("2000-01-01T00:00:00+00:00", conversation_id),
        )
        await runtime.db.commit()
        response = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "ttl-refresh", "question": "question"},
        )
        detail = await client.get(f"{BASE}/conversations/{conversation_id}")

    assert response.status_code == 200
    assert calls["resolve"] == 0
    assert detail.json()["contextVersion"] == 1
    assert detail.json()["fixedAssetNo"] == "FA-OLD"
    assert detail.json()["context"]["registryVersion"] is None


@pytest.mark.asyncio
async def test_create_and_get_include_context_matched_suggestions(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-CHIP",
        ragflow_id="doc-chip",
        equipment_id="EQ-CHIP",
        fixed_asset_no="FA-CHIP",
    )
    async with _client(runtime) as client:
        blank = await _create_conversation(client)
        with_eq = await _create_conversation(client, equipmentId="EQ-CHIP")
        blank_detail = await client.get(
            f"{BASE}/conversations/{blank['conversationId']}"
        )
        eq_detail = await client.get(
            f"{BASE}/conversations/{with_eq['conversationId']}"
        )

    assert blank["contextCompacted"] is False
    assert [item["suggestionId"] for item in blank["suggestions"]] == [
        "describe-problem"
    ]
    assert blank_detail.json()["suggestions"] == blank["suggestions"]

    assert with_eq["contextCompacted"] is False
    assert [item["suggestionId"] for item in with_eq["suggestions"]] == [
        "inspect-fault",
        "maintenance",
    ]
    assert all(
        item["contextVersion"] == with_eq["contextVersion"]
        for item in with_eq["suggestions"]
    )
    assert eq_detail.json()["suggestions"] == with_eq["suggestions"]


@pytest.mark.asyncio
async def test_patch_context_returns_fresh_suggestions_and_stales_old(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-CHIP-PATCH",
        ragflow_id="doc-chip-patch",
        equipment_id="EQ-CHIP-PATCH",
        fixed_asset_no="FA-CHIP-PATCH",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client)
        conversation_id = conversation["conversationId"]
        old_suggestion = conversation["suggestions"][0]
        patched = await client.patch(
            f"{BASE}/conversations/{conversation_id}/context",
            json={"equipmentId": "EQ-CHIP-PATCH"},
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["contextVersion"] == 1
        assert [item["suggestionId"] for item in body["suggestions"]] == [
            "inspect-fault",
            "maintenance",
        ]
        stale = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={
                "clientMessageId": "chip-stale",
                "suggestionId": old_suggestion["suggestionId"],
                "contextVersion": old_suggestion["contextVersion"],
            },
        )
        accepted = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={
                "clientMessageId": "chip-ok",
                "suggestionId": body["suggestions"][0]["suggestionId"],
                "contextVersion": body["contextVersion"],
            },
        )

    assert stale.status_code == 409
    assert stale.json()["code"] == "SUGGESTION_STALE"
    assert accepted.status_code == 200


@pytest.mark.asyncio
async def test_same_conversation_followup_projects_gateway_history(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-FOLLOW",
        ragflow_id="doc-follow",
        equipment_id="EQ-FOLLOW",
        fixed_asset_no="FA-FOLLOW",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-FOLLOW"
        )
        conversation_id = conversation["conversationId"]
        first = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "follow-1", "question": "第一轮问题"},
        )
        second = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "follow-2", "question": "第二轮续问"},
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation_id}/messages"
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["conversationId"] == conversation_id
    assert second.json()["conversationId"] == conversation_id
    roles = [item["role"] for item in history.json()["items"]]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2
    async with runtime.db.execute(
        "SELECT ragflow_chat_id, ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ragflow_session_id"]
    assert row["ragflow_session_id"] in runtime.stub._sessions
    assert runtime.stub._last_completion_body["session_id"] == row["ragflow_session_id"]
    assert runtime.stub._last_completion_body["messages"] is None
    session = runtime.stub._sessions[row["ragflow_session_id"]]
    assert session["name"].startswith(
        f"eam-biz-user-001-{conversation_id}-"
    )


@pytest.mark.asyncio
async def test_cleared_legacy_session_is_ignored(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-RESET",
        ragflow_id="doc-reset",
        equipment_id="EQ-RESET",
        fixed_asset_no="FA-RESET",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(client, equipmentId="EQ-RESET")
        conversation_id = conversation["conversationId"]
        first = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "reset-1", "question": "第一轮"},
        )
        assert first.status_code == 200
        await runtime.db.execute(
            "UPDATE ext_v2_conversation SET ragflow_session_id=? WHERE conversation_id=?",
            ("legacy-session", conversation_id),
        )
        await runtime.db.commit()
        second = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "reset-2", "question": "压缩后的新轮次"},
        )

    assert second.status_code == 200
    assert runtime.stub._last_completion_body["session_id"] == "legacy-session"
    assert "legacy-session" in runtime.stub._sessions


@pytest.mark.asyncio
async def test_conversation_is_isolated_across_business_users(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-ISO",
        ragflow_id="doc-iso",
        equipment_id="EQ-ISO",
        fixed_asset_no="FA-ISO",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-ISO"
        )
        conversation_id = conversation["conversationId"]
        first_ask = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "iso-first", "question": "用户一的问题"},
        )
        assert first_ask.status_code == 200

    other = UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-002",
        subject="biz-user-002",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )
    runtime.app.dependency_overrides[require_user_principal] = lambda: other
    async with _client(runtime) as client:
        detail = await client.get(f"{BASE}/conversations/{conversation_id}")
        ask = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={"clientMessageId": "iso-ask", "question": "不应可见"},
        )
        other_conversation = await _create_conversation(
            client, equipmentId="EQ-ISO"
        )
        other_ask = await client.post(
            f"{BASE}/conversations/{other_conversation['conversationId']}/messages",
            json={"clientMessageId": "iso-second", "question": "用户二的问题"},
        )
        listing = await client.get(f"{BASE}/conversations")

    assert detail.status_code == 404
    assert detail.json()["code"] == "CONVERSATION_NOT_FOUND"
    assert ask.status_code == 404
    assert other_ask.status_code == 200
    assert all(
        item["conversationId"] != conversation_id
        for item in listing.json()["items"]
    )
    async with runtime.db.execute(
        """SELECT conversation_id, ragflow_session_id
           FROM ext_v2_conversation
           WHERE conversation_id IN (?, ?)""",
        (conversation_id, other_conversation["conversationId"]),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 2
    assert all(row["ragflow_session_id"] for row in rows)
    assert rows[0]["ragflow_session_id"] != rows[1]["ragflow_session_id"]


@pytest.mark.asyncio
async def test_department_mismatch_still_retrieves_same_equipment(runtime):
    runtime.app.dependency_overrides[require_user_principal] = lambda: UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("3",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-DEPT-MISMATCH",
        ragflow_id="doc-dept-mismatch",
        equipment_id="EQ-DEPT-MISMATCH",
        fixed_asset_no="FA-DEPT-MISMATCH",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client,
            equipmentId="EQ-DEPT-MISMATCH",
            fixedAssetNo="FA-DEPT-MISMATCH",
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "dept-mismatch", "question": "检查"},
        )

    assert response.status_code == 200
    assert runtime.stub._last_completion_body is not None
    assert "doc-dept-mismatch" in str(
        runtime.stub._last_completion_body.get("doc_ids") or ""
    )
    assert response.json()["status"] in {"已完成", "无可靠依据"}
    assert response.json().get("citations") is not None
