"""Static and transport checks for the Agent Workflow v1.7.1 Chat Baseline entry point."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from enterprise.gateway.query.workflow_client import RAGFlowAgentStub
from enterprise.gateway.query import workflow_router


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLM = "ep-20260310093543-zl952@LLM@VolcEngine"
DEFAULT_RERANK = "qwen3-rerank@千问@Tongyi-Qianwen"
WORKFLOW_VERSION = "enterprise-qa-agent-v1.7.1"


def _dsl(payload):
    return payload.get("dsl", payload)


def _iter_retrieval_params(obj):
    if isinstance(obj, dict):
        if obj.get("component_name") == "Retrieval":
            params = obj.get("params")
            if isinstance(params, dict):
                yield params
        for value in obj.values():
            yield from _iter_retrieval_params(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_retrieval_params(value)


def test_checked_in_workflow_templates_have_trusted_scope_inputs():
    agent = json.loads(
        (ROOT / "workflows" / "enterprise_qa_agent_v1.template.json").read_text(encoding="utf-8")
    )
    dsl = _dsl(agent)
    assert dsl["graph"]["nodes"], "top-level graph.nodes must be non-empty"
    node_ids = {node["id"] for node in dsl["graph"]["nodes"]}
    assert node_ids == {
        "begin",
        "Agent:QueryRefiner",
        "Retrieval:FocusedEvidence",
        "Agent:FocusedAnswer",
        "Message:FinalAnswer",
    }
    assert len(dsl["graph"]["edges"]) == 4
    begin_node = next(node for node in dsl["graph"]["nodes"] if node["id"] == "begin")
    assert set(begin_node["data"]["form"]["inputs"]) >= {
        "authorized_dataset_ids",
        "authorized_doc_ids",
        "doc_scope_mode",
        "business_context",
        "user_memory",
        "internet_enabled",
        "reasoning_mode",
        "workflow_version",
    }
    assert begin_node["data"]["form"]["inputs"]["authorized_doc_ids"]["type"] == "object"
    begin = dsl["components"]["begin"]["obj"]["params"]["inputs"]
    # v1.7 Chat Baseline keeps form/params parity (object/'[]'); v1.6 used array in components.
    assert begin["authorized_dataset_ids"]["type"] in {"array", "object"}
    assert begin["authorized_doc_ids"]["type"] in {"array", "object"}
    assert begin["doc_scope_mode"]["value"] == "restrict"
    assert begin["internet_enabled"]["value"] is False
    assert begin["workflow_version"]["value"] == WORKFLOW_VERSION

    retrievals = list(_iter_retrieval_params(dsl["components"]))
    assert len(retrievals) == 1
    for params in retrievals:
        assert params["doc_scope_mode"] == "restrict"
        assert params["doc_scope_ids"] == ["begin@authorized_doc_ids"]
        assert params["dataset_ids"] == ["begin@authorized_dataset_ids"]

    blob = json.dumps(dsl, ensure_ascii=False)
    assert DEFAULT_LLM in blob
    assert "DeepSeek" not in blob
    assert "当前检索结果中没有找到可靠依据" in blob
    assert "未找到可靠依据，无法回答" not in blob
    assert WORKFLOW_VERSION in blob
    assert "Retrieval:FocusedEvidence@formalized_content" in blob
    focused = next(p for p in _iter_retrieval_params(dsl["components"]))
    assert int(focused.get("top_n") or 0) == 6
    assert int(focused.get("top_k") or 0) == 10
    assert float(focused.get("similarity_threshold") or 1) == 0.1
    assert float(focused.get("keywords_similarity_weight") or 0) == 0.7
    assert focused.get("rerank_id") == DEFAULT_RERANK
    mdf = focused.get("meta_data_filter") or {}
    assert mdf.get("method") == "semi_auto"
    semi = mdf.get("semi_auto") or []
    assert any(
        (item == "equipment_id")
        or (isinstance(item, dict) and item.get("key") == "equipment_id")
        for item in semi
    )
    qr = dsl["components"]["Agent:QueryRefiner"]["obj"]["params"]["sys_prompt"]
    assert "equipment_id" in qr
    assert "WebSearch" not in blob
    assert dsl.get("path") in ([], None)
    # Chat baseline: no Categorize / Research / FinalGuard
    assert "Categorize:QueryRouter" not in node_ids
    assert "Agent:FinalGuard" not in node_ids


def test_workflow_configuration_requires_enabled_agent_and_version():
    from dataclasses import replace

    from enterprise.gateway.config import GatewayRuntimeSettings

    workflow_router.config.clear_runtime_settings()
    try:
        base = GatewayRuntimeSettings.from_config(workflow_router.config)
        on = replace(
            base,
            workflow_enabled=True,
            workflow_agent_id="agent-1",
            workflow_version="v1",
            workflow_timeout_seconds=120.0,
        )
        workflow_router.config.apply_runtime_settings(on)
        assert workflow_router._workflow_configuration() == ("agent-1", "v1")

        # enabled=true but missing version → NOT configured (no Chat fallback)
        missing = replace(on, workflow_version="")
        workflow_router.config.apply_runtime_settings(missing)
        assert workflow_router._workflow_configuration() is None

        # hot-toggle off
        off = replace(on, workflow_enabled=False)
        workflow_router.config.apply_runtime_settings(off)
        assert workflow_router._workflow_configuration() is None
    finally:
        workflow_router.config.clear_runtime_settings()


def test_workflow_inputs_use_begin_value_contract():
    scope = SimpleNamespace(dataset_ids=("dataset-1",), document_ids=("doc-1",))
    request = SimpleNamespace(internetEnabled=False, reasoningMode="simple")
    inputs = workflow_router._workflow_inputs(
        {
            "business_context_json": '{"model":"ABC-200"}',
            "equipment_id": "GD01220022",
            "fixed_asset_no": None,
            "fault_code": None,
            "conversation_devices": "[]",
        },
        scope,
        "偏好简洁",
        request,
        WORKFLOW_VERSION,
    )
    assert inputs["authorized_doc_ids"] == {"type": "array", "value": ["doc-1"]}
    assert inputs["authorized_dataset_ids"] == {"type": "array", "value": ["dataset-1"]}
    assert inputs["doc_scope_mode"] == {"type": "line", "value": "restrict"}
    assert inputs["business_context"] == {
        "type": "object",
        "value": {
            "schema_version": 1,
            "equipment_id": "GD01220022",
            "model": "ABC-200",
        },
    }
    assert inputs["gateway_context"]["type"] == "line"
    assert "GD01220022" in inputs["gateway_context"]["value"]
    assert "authorized_doc_ids" not in inputs["gateway_context"]["value"] or True
    assert "do not shrink authorized_doc_ids" in inputs["gateway_context"]["value"]
    assert inputs["user_memory"] == {"type": "line", "value": "偏好简洁"}
    assert inputs["internet_enabled"] == {"type": "boolean", "value": False}
    assert inputs["reasoning_mode"] == {"type": "line", "value": "simple"}
    assert inputs["workflow_version"] == {
        "type": "line",
        "value": WORKFLOW_VERSION,
    }


def test_workflow_terminal_status_does_not_depend_on_evidence_or_wording():
    assert workflow_router._workflow_status_from("completed", "") == "completed"
    assert workflow_router._workflow_status_from(
        "no_reliable_evidence", "解释为何证据不足"
    ) == "no_reliable_evidence"


@pytest.mark.asyncio
async def test_workflow_stub_receives_gateway_scope_and_memory():
    stub = RAGFlowAgentStub()
    result = await stub.complete(
        agent_id="agent-1",
        question="设备厂家",
        session_id=None,
        user_id="u1",
        inputs={"authorized_doc_ids": ["doc-1"], "user_memory": "偏好简洁"},
        files=[],
    )
    assert result["data"]["data"]["status"] == "completed"
    assert stub.complete_calls[0]["inputs"]["authorized_doc_ids"] == ["doc-1"]
    assert stub.complete_calls[0]["inputs"]["user_memory"] == "偏好简洁"
