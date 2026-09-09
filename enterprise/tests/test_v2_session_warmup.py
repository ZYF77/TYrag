"""Create-time RAGFlow session warmup and ask-path reuse/fallback."""

from __future__ import annotations

import asyncio
from time import perf_counter

import pytest

from enterprise.gateway.db.dialect import fetchone
from enterprise.gateway.db.ops import gw_read
from enterprise.gateway.query import v2_router
from enterprise.tests.test_v2_conversation_contract import (
    BASE,
    _client,
    _create_conversation,
    _insert_document,
    runtime,
)


async def _wait_for_mapping(db, conversation_id: str, *, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await gw_read(
            db,
            fetchone,
            "SELECT ragflow_chat_id, ragflow_session_id FROM ext_v2_conversation "
            "WHERE conversation_id=?",
            (conversation_id,),
        )
        if row and row["ragflow_session_id"]:
            return dict(row)
        await asyncio.sleep(0.01)
    raise AssertionError(f"warmup mapping missing for {conversation_id}")


@pytest.mark.asyncio
async def test_create_triggers_async_warmup_without_blocking(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WARMUP-CREATE",
        ragflow_id="doc-warmup-create",
        equipment_id="EQ-WARMUP-CREATE",
        fixed_asset_no="FA-WARMUP-CREATE",
    )
    original = runtime.stub.create_session

    async def slow_create_session(chat_id, name, request_id=None):
        await asyncio.sleep(0.35)
        return await original(chat_id, name, request_id=request_id)

    runtime.stub.create_session = slow_create_session
    async with _client(runtime) as client:
        started = perf_counter()
        conversation = await _create_conversation(
            client, equipmentId="EQ-WARMUP-CREATE"
        )
        create_elapsed = perf_counter() - started
        assert create_elapsed < 0.25, create_elapsed
        assert "conversationId" in conversation
        assert conversation["status"] == "进行中"
        mapping = await _wait_for_mapping(
            runtime.db, conversation["conversationId"], timeout=2.0
        )

    assert mapping["ragflow_session_id"]
    assert mapping["ragflow_chat_id"]
    assert mapping["ragflow_session_id"] in runtime.stub._sessions
    session = runtime.stub._sessions[mapping["ragflow_session_id"]]
    assert session["name"].endswith("-warmup")


@pytest.mark.asyncio
async def test_ask_reuses_warmup_mapping_without_new_session(runtime):
    await _insert_document(
        runtime.db,
        external_id="DOC-WARMUP-HIT",
        ragflow_id="doc-warmup-hit",
        equipment_id="EQ-WARMUP-HIT",
        fixed_asset_no="FA-WARMUP-HIT",
    )
    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-WARMUP-HIT"
        )
        mapping = await _wait_for_mapping(
            runtime.db, conversation["conversationId"]
        )
        sessions_after_warmup = set(runtime.stub._sessions)
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={"clientMessageId": "warmup-hit-1", "question": "复用会话"},
        )

    assert response.status_code == 200, response.text
    assert runtime.stub._last_completion_body["session_id"] == mapping[
        "ragflow_session_id"
    ]
    assert set(runtime.stub._sessions) == sessions_after_warmup
    row = await gw_read(
        runtime.db,
        fetchone,
        "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    )
    assert row["ragflow_session_id"] == mapping["ragflow_session_id"]


@pytest.mark.asyncio
async def test_ask_falls_back_to_ensure_when_warmup_incomplete(runtime, monkeypatch):
    await _insert_document(
        runtime.db,
        external_id="DOC-WARMUP-FALLBACK",
        ragflow_id="doc-warmup-fallback",
        equipment_id="EQ-WARMUP-FALLBACK",
        fixed_asset_no="FA-WARMUP-FALLBACK",
    )

    async def hang_warmup(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(v2_router, "_warmup_ragflow_mapping", hang_warmup)

    async with _client(runtime) as client:
        conversation = await _create_conversation(
            client, equipmentId="EQ-WARMUP-FALLBACK"
        )
        # Mapping must still be absent — warmup is hung.
        row = await gw_read(
            runtime.db,
            fetchone,
            "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
            (conversation["conversationId"],),
        )
        assert row["ragflow_session_id"] is None
        response = await client.post(
            f"{BASE}/conversations/{conversation['conversationId']}/messages",
            json={
                "clientMessageId": "warmup-fallback-1",
                "question": "预热未完成也要能问",
            },
        )

    assert response.status_code == 200, response.text
    body = runtime.stub._last_completion_body
    assert body["session_id"]
    assert body["session_id"] in runtime.stub._sessions
    session = runtime.stub._sessions[body["session_id"]]
    assert "-warmup" not in session["name"]
    row = await gw_read(
        runtime.db,
        fetchone,
        "SELECT ragflow_session_id FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation["conversationId"],),
    )
    assert row["ragflow_session_id"] == body["session_id"]
