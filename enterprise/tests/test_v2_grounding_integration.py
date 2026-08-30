"""v2 session reuse and RAGFlow-owned fuse integration checks."""

from __future__ import annotations

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.ops import gw_read, gw_write

import json
from pathlib import Path

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

ROOT = Path(__file__).resolve().parents[2]


def test_advanced_rag_query_logs_are_metadata_only():
    paths = [
        "ragflow/rag/advanced_rag/agentic_rag.py",
        "ragflow/rag/advanced_rag/harness/tools/exploration.py",
        "ragflow/rag/advanced_rag/harness/tools/search.py",
        "ragflow/rag/advanced_rag/harness/tools/navigation.py",
    ]
    source = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in paths)

    assert '"{query}"' not in source
    assert '"{topic}"' not in source
    assert "keywords: {keywords}" not in source
    assert "@retrieve: {question}@{keywords}" not in source
    assert "query_chars=" in source


def _sse_answer_text(body: str) -> str:
    pieces: list[str] = []
    replaced = ""
    event = ""
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:") and event in {"answer.delta", "answer.replaced"}:
            payload = json.loads(line.split(":", 1)[1].strip() or "{}")
            content = str(payload.get("content") or "")
            if event == "answer.replaced":
                replaced = content
            else:
                pieces.append(content)
    return replaced or "".join(pieces)


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
    assert "EQ-GROUNDING-HISTORY" in body["scope_identifiers"]
    assert body["scope_identifiers"] == body["allowed_identifiers"]
    assert body["question"] == "第二轮"
    row = await gw_read(
        runtime.db,
        fetchone,
        "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    )
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
    assistant = await gw_read(
        runtime.db,
        fetchone,
        "SELECT content, status, reasoning FROM ext_v2_message WHERE role='assistant'",
    )
    assert dict(assistant) == {
        "content": expected,
        "status": "no_reliable_evidence",
        "reasoning": None,
    }


@pytest.mark.asyncio
async def test_v2_sse_completed_with_abstain_wording_is_not_replaced(runtime):
    """RF-PATCH-007: SSE wording never flips an explicit completed status."""
    await _insert_document(
        runtime.db,
        external_id="DOC-GROUNDING-SSE",
        ragflow_id="doc-grounding-sse",
        equipment_id="EQ-GROUNDING-SSE",
        fixed_asset_no="FA-GROUNDING-SSE",
    )
    runtime.stub.forced_answer = "当前检索结果中没有找到可靠依据，建议补充设备号。"
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

    expected = "当前检索结果中没有找到可靠依据，建议补充设备号。"
    assert response.status_code == 200
    assert response.text.count("event: run.started") == 1
    assert response.text.count("event: answer.delta") >= 2
    assert "event: answer.replaced" not in response.text
    assert "event: run.failed" not in response.text
    assert '"status": "已完成"' in response.text
    assert _sse_answer_text(response.text) == expected
    assert replay.text.count("event: answer.delta") == 1
    assert "event: answer.replaced" not in replay.text
    assert _sse_answer_text(replay.text) == expected


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
    row = await gw_read(runtime.db, fetchone, "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),)
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
        await gw_write(runtime.db, exec_sql, "UPDATE ext_v2_conversation SET ragflow_session_id=? WHERE conversation_id=?",
            ("legacy-session", conversation["conversationId"]),
        )
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
    assert "EQ-GNDJSON-104" in runtime.stub._last_completion_body["scope_identifiers"]
    assert (
        runtime.stub._last_completion_body["scope_identifiers"]
        == runtime.stub._last_completion_body["allowed_identifiers"]
    )


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
async def test_v2_inventory_question_fail_closed_without_catalog_rescue(runtime):
    """Upstream abstain on an inventory question stays failed closed.

    The document catalog rescue was removed: Gateway never invents an answer
    from registry file names when RAGFlow reports no_reliable_evidence.
    """
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
    runtime.stub._no_evidence = True
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
    assert body["status"] == "无可靠依据"
    assert body["answer"] == "未找到可靠依据，无法回答。"
    assert body["citations"] == []
    assert "当前知识库中该设备已有以下资料" not in body["answer"]
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
    assert _sse_answer_text(response.text) == "设备 EQ-GNDSSE-104 有发票和收据。"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasoning_mode", "expected"),
    [
        ("simple", None),
        ("low", 1),
        ("medium", 2),
        ("high", 3),
        ("ultra", 4),
    ],
)
async def test_v2_reasoning_mode_maps_into_completion_body(
    runtime, reasoning_mode, expected
):
    await _insert_document(
        runtime.db,
        external_id="DOC-REASONING-MODE",
        ragflow_id="doc-reasoning-mode",
        equipment_id="EQ-REASONING-MODE",
        fixed_asset_no="FA-REASONING-MODE",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-REASONING-MODE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": f"mode-{reasoning_mode}",
                "question": "问题",
                "reasoningMode": reasoning_mode,
            },
        )
        assert response.status_code == 200
        assert runtime.stub._last_completion_body["grounding_version"] == 1
    if expected is None:
        assert "reasoning" not in runtime.stub._last_completion_body
    else:
        assert runtime.stub._last_completion_body["reasoning"] == expected


@pytest.mark.asyncio
async def test_v2_reasoning_mode_rejects_unknown_value(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-REASONING-BAD",
        ragflow_id="doc-reasoning-bad",
        equipment_id="EQ-REASONING-BAD",
        fixed_asset_no="FA-REASONING-BAD",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-REASONING-BAD"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "mode-bad",
                "question": "问题",
                "reasoningMode": "max",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_v2_sse_does_not_replace_when_final_matches_stream(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-NO-REPLACE",
        ragflow_id="doc-1",
        equipment_id="EQ-NO-REPLACE",
        fixed_asset_no="FA-NO-REPLACE",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-NO-REPLACE"
        )
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "no-replace", "question": "问题"},
        )
        replay = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            headers={"Accept": "text/event-stream"},
            json={"clientMessageId": "no-replace", "question": "问题"},
        )
    assert response.status_code == 200
    assert response.text.count("event: answer.delta") >= 2
    assert "event: answer.replaced" not in response.text
    assert replay.text.count("event: answer.delta") == 1
    assert "event: answer.replaced" not in replay.text
