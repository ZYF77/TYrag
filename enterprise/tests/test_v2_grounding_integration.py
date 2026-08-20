"""v2 session reuse and RAGFlow-owned fuse integration checks."""

from __future__ import annotations

import json

import pytest

from enterprise.gateway.query import v2_router
from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE
from enterprise.tests.test_v2_conversation_contract import (
    BASE,
    _client,
    _create_conversation,
    _insert_document,
    runtime,
)


@pytest.mark.asyncio
async def test_v2_creates_and_reuses_ragflow_session(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-HISTORY",
        ragflow_id="doc-grounding-history",
        equipment_id="EQ-GROUNDING-HISTORY",
        fixed_asset_no="FA-GROUNDING-HISTORY",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GROUNDING-HISTORY"
        )
        first = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "history-1", "question": "第一轮"},
        )
        first_session = runtime.stub._last_completion_body["session_id"]
        second = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "history-2", "question": "第二轮"},
        )

    assert first.status_code == second.status_code == 200
    body = runtime.stub._last_completion_body
    assert first_session
    assert body["session_id"] == first_session
    assert body["messages"] is None
    assert body["store_history_messages"] is None
    assert body["pass_all_history_messages"] is None
    assert body["grounding_version"] == 1
    assert "EQ-GROUNDING-HISTORY" in body["allowed_identifiers"]
    assert body["question"] == "第二轮"
    async with runtime.db.execute(
        "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ragflow_session_id"] == body["session_id"]
    assert row["ragflow_session_id"] in runtime.stub._sessions
    session = runtime.stub._sessions[row["ragflow_session_id"]]
    assert session["name"].startswith(
        f"eam-biz-user-001-{conversation['conversationId']}-"
    )


@pytest.mark.asyncio
async def test_v2_guard_failure_from_ragflow_is_safe_json(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-FAIL",
        ragflow_id="doc-grounding-fail",
        equipment_id="EQ-GROUNDING-FAIL",
        fixed_asset_no="FA-GROUNDING-FAIL",
    )
    runtime.stub.forced_answer = "未找到可靠依据，无法回答。"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GROUNDING-FAIL"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "grounding-json-fail", "question": "问题"},
        )
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "grounding-json-fail", "question": "问题"},
        )

    expected = "未找到可靠依据，无法回答。"
    assert response.status_code == 200
    assert response.json()["answer"] == expected
    assert response.json()["status"] == "无可靠依据"
    assert response.json()["citations"] == []
    assert response.json()["reasoning"] is None
    assert replay.json()["answer"] == expected
    async with runtime.db.execute(
        "SELECT content, status, reasoning FROM ext_v2_message WHERE role='assistant'"
    ) as cursor:
        assistant = await cursor.fetchone()
    assert dict(assistant) == {
        "content": expected,
        "status": "no_reliable_evidence",
        "reasoning": None,
    }


@pytest.mark.asyncio
async def test_v2_sse_guard_is_buffered_and_fail_closed(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-SSE",
        ragflow_id="doc-grounding-sse",
        equipment_id="EQ-GROUNDING-SSE",
        fixed_asset_no="FA-GROUNDING-SSE",
    )
    runtime.stub.forced_answer = "未找到可靠依据，无法回答。"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GROUNDING-SSE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "grounding-sse-fail", "question": "问题"},
        )
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "grounding-sse-fail", "question": "问题"},
        )

    expected = "未找到可靠依据，无法回答。"
    assert response.status_code == 200
    assert response.text.count("event: run.started") == 1
    assert response.text.count("event: answer.delta") == 1
    assert "event: reasoning.delta" not in response.text
    assert f'"content": "{expected}"' in response.text
    assert "event: run.failed" not in response.text
    assert replay.text.count("event: answer.delta") == 1
    assert "event: reasoning.delta" not in replay.text


@pytest.mark.asyncio
async def test_v2_null_session_creates_new_without_backfill(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-SESSION",
        ragflow_id="doc-grounding-session",
        equipment_id="EQ-GROUNDING-SESSION",
        fixed_asset_no="FA-GROUNDING-SESSION",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GROUNDING-SESSION"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "grounding-new-session", "question": "问题"},
        )

    assert response.status_code == 200
    assert runtime.stub._last_completion_body["session_id"]
    async with runtime.db.execute(
        "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ragflow_session_id"]
    assert row["ragflow_session_id"] == runtime.stub._last_completion_body["session_id"]
    assert row["ragflow_session_id"] in runtime.stub._sessions
    session = runtime.stub._sessions[row["ragflow_session_id"]]
    assert session["name"].startswith(
        f"eam-biz-user-001-{conversation['conversationId']}-"
    )


@pytest.mark.asyncio
async def test_v2_existing_session_id_is_reused(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-REUSE",
        ragflow_id="doc-grounding-reuse",
        equipment_id="EQ-GROUNDING-REUSE",
        fixed_asset_no="FA-GROUNDING-REUSE",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GROUNDING-REUSE"
        )
        await runtime.db.execute(
            "UPDATE ext_v2_conversation SET ragflow_session_id=? WHERE conversation_id=?",
            ("legacy-session", conversation["conversationId"]),
        )
        await runtime.db.commit()
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "grounding-reuse-session", "question": "问题"},
        )

    assert response.status_code == 200
    assert runtime.stub._last_completion_body["session_id"] == "legacy-session"
    assert "legacy-session" in runtime.stub._sessions


def _assert_no_grounding_leak(payload: object) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    assert "effectiveKnowledge" not in raw
    if isinstance(payload, dict):
        assert "grounding" not in payload
        assert "effectiveKnowledge" not in payload


def test_allowed_identifiers_accept_mapping_without_get():
    class Row:
        def __getitem__(self, key):
            return {"equipment_id": "EQ-ROW-104", "fixed_asset_no": "FA-ROW-104"}[key]

    tokens = v2_router._allowed_identifiers(Row(), "请看 GI01240015")
    assert "EQ-ROW-104" in tokens
    assert "FA-ROW-104" in tokens
    assert "请看 GI01240015" in tokens


@pytest.mark.asyncio
async def test_v2_bound_equipment_id_is_sent_as_allowed_identifier(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-BOUND-JSON",
        ragflow_id="doc-grounding-bound-json",
        equipment_id="EQ-GNDJSON-104",
        fixed_asset_no="FA-GNDJSON-104",
    )
    runtime.stub.forced_answer = "设备 EQ-GNDJSON-104 有发票和收据。"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GNDJSON-104"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "grounding-bound-json",
                "question": "这个设备有哪些信息？",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "已完成"
    assert body["answer"] == "设备 EQ-GNDJSON-104 有发票和收据。"
    _assert_no_grounding_leak(body)
    assert "EQ-GNDJSON-104" in runtime.stub._last_completion_body["allowed_identifiers"]


@pytest.mark.asyncio
async def test_v2_inventory_question_keeps_listing_when_model_mixes_abstain(
    runtime,
):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-INVENTORY",
        ragflow_id="doc-grounding-inventory",
        equipment_id="EQ-GNDINV-104",
        fixed_asset_no="FA-GNDINV-104",
    )
    runtime.stub.forced_answer = (
        f"设备 EQ-GNDINV-104 现有发票和收据。[ID:0] {ABSTAIN_PHRASE}"
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GNDINV-104"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "grounding-inventory-mixed",
                "question": "GI01240015这个设备有哪些信息？",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "已完成"
    assert "发票" in body["answer"]
    assert body["answer"] != "未找到可靠依据，无法回答。"
    _assert_no_grounding_leak(body)


@pytest.mark.asyncio
async def test_v2_inventory_abstain_uses_document_catalog(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-CATALOG-INV",
        ragflow_id="doc-catalog-inv",
        equipment_id="EQ-CATALOG-104",
        fixed_asset_no="FA-CATALOG-104",
        file_name="Invoice-GTBOCLJY-0002.pdf",
    )
    await _insert_document(
        runtime.db,
        external_id="DOC-CATALOG-RCPT",
        ragflow_id="doc-catalog-rcpt",
        equipment_id="EQ-CATALOG-104",
        fixed_asset_no="FA-CATALOG-104",
        file_name="Receipt-2939-1838.pdf",
    )
    runtime.stub.forced_answer = ABSTAIN_PHRASE
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-CATALOG-104"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "grounding-catalog-inventory",
                "question": "GI01240015这个设备有哪些信息？",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "已完成"
    assert body["answer"] == "当前知识库中该设备已有以下资料：发票、收据。"
    assert "GTBOCLJY" not in body["answer"]
    _assert_no_grounding_leak(body)


@pytest.mark.asyncio
async def test_v2_bound_equipment_sse_forwards_grounded_answer(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-BOUND-SSE",
        ragflow_id="doc-grounding-bound-sse",
        equipment_id="EQ-GNDSSE-104",
        fixed_asset_no="FA-GNDSSE-104",
    )
    runtime.stub.forced_answer = "设备 EQ-GNDSSE-104 有发票和收据。"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GNDSSE-104"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={
                "clientMessageId": "grounding-bound-sse",
                "question": "这个设备有哪些信息？",
            },
        )

    assert response.status_code == 200
    assert '"status": "已完成"' in response.text
    assert "设备 EQ-GNDSSE-104 有发票和收据。" in response.text
    assert "未找到可靠依据，无法回答。" not in response.text
    _assert_no_grounding_leak(response.text)


@pytest.mark.asyncio
async def test_v2_unrelated_fault_code_abstain_from_ragflow(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-UNRELATED",
        ragflow_id="doc-grounding-unrelated",
        equipment_id="EQ-GNDUNREL-104",
        fixed_asset_no="FA-GNDUNREL-104",
    )
    runtime.stub.forced_answer = "未找到可靠依据，无法回答。"
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-GNDUNREL-104"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "grounding-unrelated-fault",
                "question": "故障码是什么？",
            },
        )

    expected = "未找到可靠依据，无法回答。"
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "无可靠依据"
    assert body["answer"] == expected
    _assert_no_grounding_leak(body)
