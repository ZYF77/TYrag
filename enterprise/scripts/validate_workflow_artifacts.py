"""Validate checked-in TYrag Agent/Pipeline templates without contacting services."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_DEFAULT_CHAT_LLM_ID = "ep-20260310093543-zl952@LLM@VolcEngine"
_DEFAULT_RERANK_ID = "qwen3-rerank@千问@Tongyi-Qianwen"
_WORKFLOW_VERSION = "enterprise-qa-agent-v1.7.1"
_REQUIRED_AGENT_NODES = {
    "begin",
    "Agent:QueryRefiner",
    "Retrieval:FocusedEvidence",
    "Agent:FocusedAnswer",
    "Message:FinalAnswer",
}


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    # Agent uploads use RAGFlow's canonical wire shape (graph/components at
    # the top level). Keep accepting the older wrapper used by the ingestion
    # artifact so this validator can check both checked-in canvas formats.
    dsl = value.get("dsl") if isinstance(value.get("dsl"), dict) else value
    components = dsl.get("components")
    if not isinstance(components, dict) or not components:
        raise ValueError(f"{path}: components must be non-empty")
    return dsl


def _iter_retrieval_params(obj, path=""):
    if isinstance(obj, dict):
        if obj.get("component_name") == "Retrieval":
            params = obj.get("params")
            if isinstance(params, dict):
                yield path or "Retrieval", params
        for key, value in obj.items():
            yield from _iter_retrieval_params(value, f"{path}/{key}" if path else key)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _iter_retrieval_params(value, f"{path}[{index}]")


def _authorized_ok(field: dict) -> bool:
    """Accept v1.6 array=[] or v1.7 Chat-baseline object/'[]' Begin defaults."""
    if not isinstance(field, dict):
        return False
    t = field.get("type")
    v = field.get("value")
    if t == "array":
        return v == [] or v == "[]"
    if t == "object":
        if v == []:
            return True
        if isinstance(v, str):
            try:
                return json.loads(v) == []
            except json.JSONDecodeError:
                return False
    return False


def validate(agent_path: Path, pipeline_path: Path) -> None:
    agent = _load(agent_path)
    pipeline = _load(pipeline_path)

    dsl = agent
    graph = dsl.get("graph")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ValueError(f"{agent_path}: Agent import must contain top-level graph.nodes")
    if not graph["nodes"]:
        raise ValueError(f"{agent_path}: graph.nodes must be non-empty")
    node_ids = {node.get("id") for node in graph["nodes"] if isinstance(node, dict)}
    missing = _REQUIRED_AGENT_NODES - node_ids
    if missing:
        raise ValueError(f"{agent_path}: required Agent graph nodes missing: {sorted(missing)}")
    if node_ids != _REQUIRED_AGENT_NODES:
        extra = node_ids - _REQUIRED_AGENT_NODES
        raise ValueError(
            f"{agent_path}: v1.7 Chat Baseline must be exactly 5 nodes; extra={sorted(extra)}"
        )
    if not isinstance(graph.get("edges"), list) or len(graph["edges"]) != 4:
        raise ValueError(f"{agent_path}: v1.7 Chat Baseline must contain exactly 4 edges")

    begin_graph_inputs = (
        next(node for node in graph["nodes"] if node.get("id") == "begin")
        .get("data", {})
        .get("form", {})
        .get("inputs", {})
    )
    allowed_begin_types = {"line", "paragraph", "options", "file", "integer", "boolean", "object"}
    if not isinstance(begin_graph_inputs, dict) or not begin_graph_inputs:
        raise ValueError(f"{agent_path}: Begin graph inputs must be non-empty")
    for key, field in begin_graph_inputs.items():
        if field.get("type") not in allowed_begin_types:
            raise ValueError(f"{agent_path}: unsupported Begin graph input type for {key!r}")
    if "authorized_doc_ids" not in begin_graph_inputs:
        raise ValueError(f"{agent_path}: Begin graph inputs must include authorized_doc_ids")

    components = dsl["components"]
    begin = components.get("begin", {}).get("obj", {}).get("params", {}).get("inputs", {})
    for key in (
        "authorized_dataset_ids",
        "authorized_doc_ids",
        "doc_scope_mode",
        "business_context",
        "user_memory",
        "internet_enabled",
        "reasoning_mode",
        "workflow_version",
    ):
        if key not in begin:
            raise ValueError(f"{agent_path}: Begin input {key!r} is missing")
    if not _authorized_ok(begin.get("authorized_dataset_ids") or {}):
        raise ValueError(f"{agent_path}: authorized_dataset_ids default must be empty array")
    if not _authorized_ok(begin.get("authorized_doc_ids") or {}):
        raise ValueError(f"{agent_path}: authorized_doc_ids default must be empty array")
    if begin.get("doc_scope_mode", {}).get("value") != "restrict":
        raise ValueError(f"{agent_path}: Begin doc_scope_mode default must be restrict")
    if begin.get("internet_enabled", {}).get("value") is not False:
        raise ValueError(f"{agent_path}: internet_enabled must default to false")
    if begin.get("workflow_version", {}).get("value") != _WORKFLOW_VERSION:
        raise ValueError(
            f"{agent_path}: workflow_version must default to {_WORKFLOW_VERSION}"
        )

    retrievals = list(_iter_retrieval_params(components))
    if len(retrievals) != 1:
        raise ValueError(
            f"{agent_path}: v1.7 Chat Baseline expects exactly 1 Retrieval, found {len(retrievals)}"
        )
    for label, params in retrievals:
        if params.get("doc_scope_mode") != "restrict":
            raise ValueError(f"{agent_path}: {label} must use doc_scope_mode=restrict")
        if params.get("doc_scope_ids") != ["begin@authorized_doc_ids"]:
            raise ValueError(f"{agent_path}: {label} must consume begin@authorized_doc_ids")
        if params.get("dataset_ids") != ["begin@authorized_dataset_ids"]:
            raise ValueError(f"{agent_path}: {label} must consume begin@authorized_dataset_ids")

    serialized = json.dumps(dsl, ensure_ascii=False)
    if "DeepSeek" in serialized or "deepseek" in serialized.lower():
        raise ValueError(f"{agent_path}: DeepSeek llm_id is forbidden; use VolcEngine 豆包")
    if re.search(r'"component_name"\s*:\s*"WebSearch"', serialized) or re.search(
        r'"id"\s*:\s*"(?:Tool:)?WebSearch"', serialized
    ):
        raise ValueError(f"{agent_path}: Web Search must not be registered in v1.7 template")
    if _DEFAULT_CHAT_LLM_ID not in serialized and "__REPLACE_WITH_CHAT_LLM_ID__" not in serialized:
        raise ValueError(
            f"{agent_path}: expected default VolcEngine llm_id {_DEFAULT_CHAT_LLM_ID!r} "
            "or prepare placeholder"
        )
    if "当前检索结果中没有找到可靠依据" not in serialized:
        raise ValueError(f"{agent_path}: Chat v12 fixed refusal copy is missing")
    if "未找到可靠依据，无法回答" in serialized:
        raise ValueError(f"{agent_path}: forbidden legacy abstain copy must not appear")
    if _WORKFLOW_VERSION not in serialized:
        raise ValueError(f"{agent_path}: workflow_version must default to {_WORKFLOW_VERSION}")

    # Retrieval Chat-align pins (FocusedEvidence)
    focused = (
        ((dsl.get("components") or {}).get("Retrieval:FocusedEvidence") or {})
        .get("obj", {})
        .get("params", {})
    )
    if int(focused.get("top_n") or 0) != 6:
        raise ValueError(f"{agent_path}: FocusedEvidence top_n must be 6 (Chat align)")
    if int(focused.get("top_k") or 0) != 10:
        raise ValueError(f"{agent_path}: FocusedEvidence top_k must be 10 (Chat align)")
    if float(focused.get("similarity_threshold") or 1) != 0.1:
        raise ValueError(f"{agent_path}: FocusedEvidence similarity_threshold must be 0.1")
    if float(focused.get("keywords_similarity_weight") or 0) != 0.7:
        raise ValueError(f"{agent_path}: FocusedEvidence keywords_similarity_weight must be 0.7")
    if focused.get("rerank_id") != _DEFAULT_RERANK_ID:
        raise ValueError(f"{agent_path}: FocusedEvidence rerank_id must be {_DEFAULT_RERANK_ID}")
    mdf = focused.get("meta_data_filter") or {}
    if not isinstance(mdf, dict) or mdf.get("method") != "semi_auto":
        raise ValueError(
            f"{agent_path}: meta_data_filter.method must be semi_auto (not empty/auto/manual)"
        )
    if mdf.get("method") == "auto":
        raise ValueError(f"{agent_path}: meta_data_filter full auto is forbidden")
    semi = mdf.get("semi_auto") or []
    if not isinstance(semi, list) or not semi:
        raise ValueError(f"{agent_path}: meta_data_filter.semi_auto must be non-empty")
    has_equipment = any(
        (item == "equipment_id")
        or (isinstance(item, dict) and item.get("key") == "equipment_id")
        for item in semi
    )
    if not has_equipment:
        raise ValueError(f"{agent_path}: meta_data_filter.semi_auto must include equipment_id")
    # Forbid hardcoding real KB UUIDs / display-name bindings in dataset_ids
    for label, params in retrievals:
        for did in params.get("dataset_ids") or []:
            if isinstance(did, str) and did != "begin@authorized_dataset_ids":
                raise ValueError(
                    f"{agent_path}: {label} dataset_ids must be exactly ['begin@authorized_dataset_ids']; got {did!r}"
                )
            if isinstance(did, str) and " " in did:
                raise ValueError(
                    f"{agent_path}: {label} dataset_ids must not use display-name space binding"
                )
    qr_prompt = (
        ((dsl.get("components") or {}).get("Agent:QueryRefiner") or {})
        .get("obj", {})
        .get("params", {})
        .get("sys_prompt")
        or ""
    )
    if "equipment_id" not in qr_prompt:
        raise ValueError(
            f"{agent_path}: QueryRefiner sys_prompt must pin equipment_id for semi_auto"
        )
    fa_blob = json.dumps(
        ((dsl.get("components") or {}).get("Agent:FocusedAnswer") or {}),
        ensure_ascii=False,
    )
    if "Retrieval:FocusedEvidence@formalized_content" not in fa_blob:
        raise ValueError(f"{agent_path}: FocusedAnswer must bind formalized_content")
    if "{begin@user_memory}" not in fa_blob:
        raise ValueError(f"{agent_path}: FocusedAnswer must bind begin@user_memory")

    # path must be empty list for RF import
    if dsl.get("path") not in ([], None):
        raise ValueError(f"{agent_path}: dsl.path must be [] for import")

    pipeline_components = pipeline["components"]
    parser = pipeline_components["Parser:EnterpriseFormats"]["obj"]["params"]
    if parser.get("json", {}).get("suffix") != ["json", "jsonl", "ldjson"]:
        raise ValueError(f"{pipeline_path}: JSON/JSONL parser setup is missing")
    if pipeline_components["TokenChunker:EnterpriseChunks"]["downstream"] != [
        "Tokenizer:EnterpriseIndexInput"
    ]:
        raise ValueError(f"{pipeline_path}: TokenChunker must feed Tokenizer")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent",
        type=Path,
        default=Path("enterprise/workflows/enterprise_qa_agent_v1.template.json"),
    )
    parser.add_argument(
        "--pipeline",
        type=Path,
        default=Path("enterprise/workflows/eam_ingestion_pipeline_v1.json"),
    )
    args = parser.parse_args()
    validate(args.agent, args.pipeline)
    print("workflow-artifacts-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
