"""Conversation history behavior while Gateway compression remains deferred."""

from __future__ import annotations

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.ops import gw_read, gw_write

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config
from enterprise.gateway.query import formal_router, v2_router
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
    monkeypatch.setattr(config, "context_compress_enabled", True)
    monkeypatch.setattr(config, "context_compress_turns", 4)
    monkeypatch.setattr(config, "context_compress_keep_recent", 2)
    monkeypatch.setattr(config, "context_summary_max_chars", 1500)
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


async def _seed_doc(db) -> None:
    await gw_write(db, insert_mapping,
        ExtDocumentMap(
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-COMPRESS",
            source_version_id="v1",
            event_id=str(uuid.uuid4()),
            sha256=hashlib.sha256(b"DOC-COMPRESS").hexdigest(),
            file_name="DOC-COMPRESS.pdf",
            asset_id="FA-COMPRESS",
            equipment_id="EQ-COMPRESS",
            fixed_asset_no="FA-COMPRESS",
            department_id="d10",
            security_level=2,
            allow_group_ids=json.dumps(["maintenance"]),
            deny_group_ids="[]",
            ragflow_dataset_id="ds-compress",
            ragflow_document_id="doc-compress",
            sync_status="ready",
            pipeline_status="DONE",
            business_status="active",
            current_version=1,
        ),
    )


@pytest.mark.asyncio
async def test_stateless_history_keeps_raw_turns_without_summary(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-COMPRESS"}
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversationId"]
        assert created.json()["contextCompacted"] is False
        for index in range(2):
            response = await client.post(
                f"{BASE}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": f"compress-{index}",
                    "question": f"压缩轮次问题-{index}",
                },
            )
            assert response.status_code == 200, response.text

        detail = await client.get(f"{BASE}/conversations/{conversation_id}")
        history = await client.get(
            f"{BASE}/conversations/{conversation_id}/messages?limit=50"
        )

    assert detail.status_code == 200
    assert detail.json()["contextCompacted"] is False
    assert detail.json()["conversationId"] == conversation_id
    row = await gw_read(runtime.db, fetchone, "SELECT context_summary, ragflow_session_id, compressed_turn_watermark "
        "FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation_id,),)
    assert row["context_summary"] is None
    assert row["ragflow_session_id"]
    assert int(row["compressed_turn_watermark"]) == 0
    user_contents = [
        item["content"]
        for item in history.json()["items"]
        if item["role"] == "user"
    ]
    assert user_contents == ["压缩轮次问题-0", "压缩轮次问题-1"]


@pytest.mark.asyncio
async def test_next_ask_projects_prior_turns_without_summary_prefix(runtime):
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-COMPRESS"}
        )
        conversation_id = created.json()["conversationId"]
        for index in range(2):
            await client.post(
                f"{BASE}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": f"prefix-{index}",
                    "question": f"前缀轮次-{index}",
                },
            )
        follow = await client.post(
            f"{BASE}/conversations/{conversation_id}/messages",
            json={
                "clientMessageId": "prefix-follow",
                "question": "压缩后续问",
            },
        )
        history = await client.get(
            f"{BASE}/conversations/{conversation_id}/messages?limit=50"
        )

    assert follow.status_code == 200
    body = runtime.stub._last_completion_body
    assert body is not None
    assert body["question"] == "压缩后续问"
    assert body["messages"] is None
    assert body["session_id"]
    user_contents = [
        item["content"]
        for item in history.json()["items"]
        if item["role"] == "user"
    ]
    assert "压缩后续问" in user_contents
    assert all("[先前对话摘要]" not in content for content in user_contents)


@pytest.mark.asyncio
async def test_compress_failure_does_not_break_completed_answer(
    runtime, monkeypatch
):
    await _seed_doc(runtime.db)

    async def boom(*args, **kwargs):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(
        "enterprise.gateway.query.context_compress.v2_store.save_context_summary",
        boom,
    )
    async with _client(runtime) as client:
        created = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-COMPRESS"}
        )
        conversation_id = created.json()["conversationId"]
        last = None
        for index in range(2):
            last = await client.post(
                f"{BASE}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": f"fail-{index}",
                    "question": f"失败压缩-{index}",
                },
            )
            assert last.status_code == 200

    assert last.json()["status"] in {"已完成", "无可靠依据"}
    row = await gw_read(runtime.db, fetchone, "SELECT context_summary FROM ext_v2_conversation WHERE conversation_id=?",
        (conversation_id,),)
    assert not row["context_summary"]


@pytest.mark.asyncio
async def test_compress_disabled_skips_summary(runtime, monkeypatch):
    monkeypatch.setattr(config, "context_compress_enabled", False)
    await _seed_doc(runtime.db)
    async with _client(runtime) as client:
        created = await client.post(
            f"{BASE}/conversations", json={"equipmentId": "EQ-COMPRESS"}
        )
        conversation_id = created.json()["conversationId"]
        for index in range(2):
            response = await client.post(
                f"{BASE}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": f"off-{index}",
                    "question": f"关闭压缩-{index}",
                },
            )
            assert response.status_code == 200
        detail = await client.get(f"{BASE}/conversations/{conversation_id}")

    assert detail.json()["contextCompacted"] is False
    row = await gw_read(runtime.db, fetchone, "SELECT context_summary, ragflow_session_id FROM ext_v2_conversation "
        "WHERE conversation_id=?",
        (conversation_id,),)
    assert row["context_summary"] is None
    assert row["ragflow_session_id"]


@pytest.mark.asyncio
async def test_config_defaults_for_context_compress():
    cfg_defaults = type(config)()
    assert cfg_defaults.context_compress_enabled is True
    assert cfg_defaults.context_compress_turns == 20
    assert cfg_defaults.context_summary_max_chars == 1500
