"""Equipment Identity Metadata & Prompt — unit tests."""

from __future__ import annotations

import pytest

from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE
from enterprise.gateway.query.enterprise_prompt import (
    ENTERPRISE_PROMPT_MARKER,
    REFERENCE_METADATA_FIELDS,
    build_enterprise_prompt_config,
    needs_enterprise_prompt_upgrade,
)
from enterprise.gateway.query.formal_router import _ensure_chat
from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub
from enterprise.gateway.query.v2_router import GLOBAL_QUESTION_PREFIX, _ragflow_question
from enterprise.gateway.sync.models import ExtDocumentMap, OutboxEvent
from enterprise.gateway.sync.sync_service import SyncService


def test_enterprise_prompt_has_knowledge_and_two_tier_relevance():
    cfg = build_enterprise_prompt_config()
    system = cfg["system"]

    assert "{knowledge}" in system
    assert ENTERPRISE_PROMPT_MARKER in system
    assert "document_metadata" in system
    assert "equipment_id" in system
    assert "fixed_asset_no" in system
    assert "设备归属不等于内容与问题相关" in system
    assert "不能证明" in system and "正文包含用户当前问题需要的事实" in system
    assert "不得根据设备归属推测" in system
    assert ABSTAIN_PHRASE in system
    assert "禁止任何 [ID:n]" in system
    assert "禁止把合格证、调试记录等无关文档当对照证据引用" in system
    assert "有哪些资料/文档" in system
    assert "必须在正文用方括号引用格式 [ID:n]" in system
    assert "半支撑" in system
    assert "禁止沿用上一轮" in system
    assert "必须标 [ID:0]" in system
    assert "<think>" in system and "</think>" in system
    assert "标签外只写给用户看的最终正文" in system
    assert "尽量回答" not in system
    assert "not found in the dataset" not in system.lower()
    assert "附件观察与知识库事实必须分叉" in system
    assert "禁止因此写约定拒答句" in system
    assert "禁止把附件观察写成台账事实" in system
    assert "无附件时，empty_response 与拒答规则保持不变" in system

    ref = cfg["reference_metadata"]
    assert ref["include"] is True
    assert ref["fields"] == list(REFERENCE_METADATA_FIELDS)


def test_needs_enterprise_prompt_upgrade_detects_default_chat():
    assert needs_enterprise_prompt_upgrade({}) is True
    assert needs_enterprise_prompt_upgrade({"prompt_config": {}}) is True
    assert needs_enterprise_prompt_upgrade({
        "prompt_config": {
            "system": "You are an intelligent assistant. {knowledge}",
        }
    }) is True
    upgraded = {"prompt_config": build_enterprise_prompt_config()}
    assert needs_enterprise_prompt_upgrade(upgraded) is False
    for legacy_marker in (
        "enterprise_identity_metadata_v2",
        "enterprise_identity_metadata_v3",
        "enterprise_identity_metadata_v4",
        "enterprise_identity_metadata_v5",
        "enterprise_identity_metadata_v6",
    ):
        legacy = build_enterprise_prompt_config()
        legacy["system"] = legacy["system"].replace(
            ENTERPRISE_PROMPT_MARKER, legacy_marker
        )
        assert needs_enterprise_prompt_upgrade({"prompt_config": legacy}) is True


def test_ragflow_question_never_adds_gateway_identity_prefix():
    bound = {"equipment_id": "GI01250001", "fixed_asset_no": "FA-1"}
    unbound = {"equipment_id": None, "fixed_asset_no": None}

    assert _ragflow_question(bound, "漏气维修记录") == "漏气维修记录"
    assert _ragflow_question(unbound, "漏气维修记录") == "漏气维修记录"
    assert GLOBAL_QUESTION_PREFIX not in _ragflow_question(unbound, "漏气维修记录")
    # User-authored equipment id in the question is preserved as-is.
    assert _ragflow_question(bound, "知识库里 GI01250001 有哪些信息") == (
        "知识库里 GI01250001 有哪些信息"
    )


@pytest.mark.asyncio
async def test_ensure_chat_writes_prompt_config_on_create():
    client = RAGFlowQueryStub()
    principal = type("P", (), {"tenant_id": "tenant-a"})()
    scope = type("S", (), {"dataset_ids": ("ds-1",)})()

    chat_id = await _ensure_chat(client, principal, scope)

    chat = client._chats[chat_id]
    assert chat["name"] == "enterprise-formal-tenant-a"
    assert chat["dataset_ids"] == ["ds-1"]
    assert needs_enterprise_prompt_upgrade(chat) is False
    assert chat["prompt_config"]["reference_metadata"]["fields"] == list(
        REFERENCE_METADATA_FIELDS
    )


@pytest.mark.asyncio
async def test_ensure_chat_does_not_patch_legacy_prompt_config():
    client = RAGFlowQueryStub()
    principal = type("P", (), {"tenant_id": "tenant-b"})()
    scope = type("S", (), {"dataset_ids": ("ds-1", "ds-2")})()
    updates: list[tuple[tuple, dict]] = []
    original_update = client.update_chat

    async def tracking_update(*args, **kwargs):
        updates.append((args, kwargs))
        return await original_update(*args, **kwargs)

    client.update_chat = tracking_update

    created = await client.create_chat(
        "enterprise-formal-tenant-b",
        ["ds-1"],
    )
    chat_id = created["data"]["id"]
    assert needs_enterprise_prompt_upgrade(client._chats[chat_id]) is True

    ensured = await _ensure_chat(client, principal, scope)
    assert ensured == chat_id
    chat = client._chats[chat_id]
    assert set(chat["dataset_ids"]) == {"ds-1", "ds-2"}
    assert "prompt_config" not in chat
    assert needs_enterprise_prompt_upgrade(chat) is True
    assert len(updates) == 1
    args, kwargs = updates[0]
    assert kwargs.get("prompt_config") is None
    assert not any(isinstance(arg, dict) and "system" in arg for arg in args)


@pytest.mark.asyncio
async def test_ensure_chat_preserves_ragflow_edited_system_prompt():
    client = RAGFlowQueryStub()
    principal = type("P", (), {"tenant_id": "tenant-c"})()
    scope = type("S", (), {"dataset_ids": ("ds-1", "ds-2")})()
    custom_system = "You are a custom RAGFlow assistant. {knowledge}"
    updates: list[tuple[tuple, dict]] = []
    original_update = client.update_chat

    async def tracking_update(*args, **kwargs):
        updates.append((args, kwargs))
        return await original_update(*args, **kwargs)

    client.update_chat = tracking_update

    created = await client.create_chat(
        "enterprise-formal-tenant-c",
        ["ds-1"],
        prompt_config={"system": custom_system},
    )
    chat_id = created["data"]["id"]

    ensured = await _ensure_chat(client, principal, scope)
    assert ensured == chat_id
    chat = client._chats[chat_id]
    assert chat["prompt_config"]["system"] == custom_system
    assert set(chat["dataset_ids"]) == {"ds-1", "ds-2"}
    assert len(updates) == 1
    args, kwargs = updates[0]
    assert kwargs.get("prompt_config") is None
    assert not any(isinstance(arg, dict) and "system" in arg for arg in args)


def test_external_meta_fields_include_canonical_identity_not_ocr_ground_truth():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-ID-1",
        source_version_id="v1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name="invoice.pdf",
        document_type="invoice",
        equipment_id="GI01250001",
        fixed_asset_no="FA-GI01250001",
    )
    event = OutboxEvent(
        event_id=doc.event_id,
        event_type="upsert",
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        payload="{}",
    )

    meta = SyncService._external_meta_fields(doc, event)

    assert meta["equipment_id"] == "GI01250001"
    assert meta["fixed_asset_no"] == "FA-GI01250001"
    assert meta["enterprise_document_type"] == "invoice"
    assert meta["enterprise_external_document_id"] == "DOC-ID-1"
    assert meta["enterprise_quality_ground_truth_json"] == "{}"
    assert "equipment_id" not in meta["enterprise_quality_ground_truth_json"]


def test_external_meta_fields_omit_empty_identity():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-ID-2",
        source_version_id="v1",
        event_id="evt-2",
        sha256="b" * 64,
        file_name="scan.pdf",
        document_type="PRODUCT_MANUAL",
        equipment_id=None,
        fixed_asset_no="",
    )
    event = OutboxEvent(
        event_id=doc.event_id,
        event_type="upsert",
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        payload="{}",
    )

    meta = SyncService._external_meta_fields(doc, event)

    assert "equipment_id" not in meta
    assert "fixed_asset_no" not in meta
