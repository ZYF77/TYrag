"""Equipment Identity Metadata & Prompt — unit tests."""

from __future__ import annotations

import pytest

from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE
from enterprise.gateway.query.enterprise_prompt import (
    ENTERPRISE_PROMPT_MARKER,
    REFERENCE_METADATA_FIELDS,
    apply_inventory_rule_to_operator_prompt,
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
    assert ENTERPRISE_PROMPT_MARKER == "enterprise_identity_metadata_v9"
    assert ENTERPRISE_PROMPT_MARKER in system
    assert "document_metadata" in system
    assert "equipment_id" in system
    assert "fixed_asset_no" in system
    assert "正文未出现设备编号" in system
    assert "metadata 只证明文档归属" in system
    assert "必须由 Content 实际内容支持" in system
    assert ABSTAIN_PHRASE in system
    assert cfg["empty_response"] == ABSTAIN_PHRASE
    assert "优先回答可靠信息能够支持的部分" in system
    assert "metadata 缺字段或引用格式问题" in system
    assert "核心事实完全没有可靠依据" in system
    assert "现有资料、文档、文件或内容" in system
    assert "不要报告检索条数" in system
    assert "不要使用数字序号" in system
    assert "不得编造、猜测、补全或错误组合" in system
    assert "比例或统计结论" in system
    assert "[ID:n]" in system
    assert "只能使用本轮实际提供的 ID" in system
    assert "纯附件回答和无可靠依据的拒答不使用" in system
    assert "<think>" not in system
    assert "不得假装已读取" in system
    assert "TXT/PDF" not in system
    assert "JPEG" not in system
    assert "## " not in system

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
        "enterprise_identity_metadata_v7",
        "enterprise_identity_metadata_v8",
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


def test_apply_inventory_rule_keeps_operator_attachment_sections():
    original = (
        "临时附件规则保留。\n"
        "[enterprise_identity_metadata_v6]\n"
        "用户问「有哪些资料/文档」且概括真实命中内容时，可以引用并标 `[ID:n]`。\n"
        "{knowledge}"
    )
    patched = apply_inventory_rule_to_operator_prompt(original)
    assert "临时附件规则保留" in patched
    assert "[enterprise_identity_metadata_v6]" in patched
    assert "现有单据类型本身就是答案" in patched
    assert "有哪些信息/资料/文档/文件/内容" in patched
    assert apply_inventory_rule_to_operator_prompt(patched) == patched


@pytest.mark.asyncio
async def test_ensure_chat_preserves_v6_operator_prompt():
    client = RAGFlowQueryStub()
    principal = type("P", (), {"tenant_id": "tenant-v6"})()
    scope = type("S", (), {"dataset_ids": ("ds-1", "ds-2")})()
    operator_system = (
        "你是企业设备知识库助手。[enterprise_identity_metadata_v6]\n"
        "临时附件规则…… {knowledge}"
    )
    updates: list[tuple[tuple, dict]] = []
    original_update = client.update_chat

    async def tracking_update(*args, **kwargs):
        updates.append((args, kwargs))
        return await original_update(*args, **kwargs)

    client.update_chat = tracking_update
    created = await client.create_chat(
        "enterprise-formal-tenant-v6",
        ["ds-1"],
        prompt_config={"system": operator_system},
    )
    chat_id = created["data"]["id"]

    ensured = await _ensure_chat(client, principal, scope)
    assert ensured == chat_id
    chat = client._chats[chat_id]
    assert chat["prompt_config"]["system"] == operator_system
    assert "enterprise_identity_metadata_v9" not in chat["prompt_config"]["system"]
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
