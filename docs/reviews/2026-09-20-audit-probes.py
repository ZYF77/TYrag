"""Service-free review probes; synthetic fixtures, not integration/E2E tests.

Run: python3 docs/reviews/2026-09-20-audit-probes.py
Actual functions/methods are compiled from repository AST without importing
application configuration or third-party runtimes. I/O collaborators are fakes.
Assertions document observed defects at the reviewed commit or fixed invariants
where the review has since applied a minimal correction.
No .env, credentials, customer files, services or network are accessed.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
import re
import sys
from time import perf_counter
from types import SimpleNamespace as NS
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def extract(path, names, namespace, class_name=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    nodes = tree.body
    if class_name:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name).body
    selected = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(selected) == len(names), (path, names)
    for node in selected:
        node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace


async def noop_async(*args, **kwargs):
    return None


def noop(*args, **kwargs):
    return None


async def retrieval_empty_metadata_probe():
    calls, outputs, references = [], {}, []
    async def metadata(*args, **kwargs):
        return []  # valid metadata predicate, zero matches within G
    async def retrieve(*args, **kwargs):
        calls.append(kwargs["doc_ids"])
        # This collaborator must not be reached after a restricted zero match.
        return {"chunks": [{"doc_id": "allowed-a", "content": "synthetic A"},
                           {"doc_id": "outside-g", "content": "synthetic outside"}], "doc_aggs": []}
    ns = {"re": re, "json": json, "apply_meta_data_filter": metadata,
          "KnowledgebaseService": NS(get_by_ids=lambda ids: [NS(id="kb", tenant_id="t", embd_id="embedding")]),
          "resolve_model_config": lambda *a: None, "LLMType": NS(EMBEDDING="embedding"),
          "LLMBundle": lambda *a: None, "label_question": lambda *a: {},
          "settings": NS(retriever=NS(retrieval=retrieve, retrieval_by_children=lambda c, t: c)),
          "kb_prompt": lambda info, *a: [c["content"] for c in info["chunks"]]}
    extract("ragflow/agent/tools/retrieval.py", {"_retrieve_kb", "_filter_kbinfos_to_scope"}, ns, "Retrieval")
    node = NS(_dataset_ids=["kb"], _resolved_doc_scope_ids=lambda: ["allowed-a"],
              _canvas=NS(get_tenant_id=lambda: "t", add_reference=lambda c, a: references.extend(c)),
              _param=NS(doc_scope_mode="restrict", empty_response="", rerank_id="",
                        meta_data_filter={"method": "manual", "manual": [{"key": "equipment_id", "op": "=", "value": "missing"}]},
                        cross_languages=[], top_n=6, similarity_threshold=.1,
                        keywords_similarity_weight=.7, top_k=10, toc_enhance=False, use_kg=False),
              get_input_elements_from_text=lambda q: {}, string_format=lambda q, v: q,
              check_if_canceled=lambda *a: False, set_output=lambda k, v: outputs.update({k: v}),
              _filter_kbinfos_to_scope=ns["_filter_kbinfos_to_scope"])
    await ns["_retrieve_kb"](node, "synthetic missing equipment")
    assert calls == []
    assert references == []
    assert outputs == {"formalized_content": "", "json": []}
    return {"retriever_not_called_on_restricted_zero_match": True, "empty_outputs_emitted": True}


def workflow_event_probe(frames):
    """Exercise the same collector used by both Gateway JSON and SSE paths."""
    from enterprise.gateway.query.workflow_events import WorkflowEventCollector, WorkflowEventError

    collector = WorkflowEventCollector()
    try:
        for frame in frames:
            collector.consume(frame)
        result = collector.finalize()
        return {
            "terminal_event": result["event"],
            "status": result["data"]["status"],
            "content": result["data"]["content"],
            "node_errors": result["data"]["_workflow_node_errors"],
        }
    except WorkflowEventError as exc:
        return {"error_code": exc.code, "status_code": exc.status_code}


async def main():
    observations = {}
    statements = []
    async def sql(conn, statement, params):
        statements.append(statement)
        return NS(rowcount=0)  # running lease has NOT expired
    async def fetch(conn, statement, params):
        statements.append(statement)
        assert "RETURNING assistant_message_id" in statement
        return None  # conditional UPDATE did not transition a live run
    async def get_run(*args, **kwargs):
        return {"status": "running"}
    ns = {"lock_conversation": noop_async, "quarantine_conversation": noop_async, "utc_now": lambda: "2026-09-20T00:00:00Z", "json": json,
          "exec_sql": sql, "fetchone": fetch, "get_message_run": get_run}
    extract("enterprise/gateway/query/v2_store.py", {"mark_expired_run_interrupted"}, ns)
    run = await ns["mark_expired_run_interrupted"](None, conversation_id="c",
        tenant_id="t", business_user_id="u", client_message_id="client")
    assert run["status"] == "running" and not any("INSERT INTO ext_v2_message" in s for s in statements)
    observations["pending_retry_preserves_running"] = {
        "lease_expiry_update_rows": 0, "run_status": run["status"], "failed_message_insert_attempted": False}
    import importlib.util
    parent_spec = importlib.util.spec_from_file_location("parent_probe", ROOT / "ragflow/rag/utils/parent_chunks.py")
    parent_module = importlib.util.module_from_spec(parent_spec)
    parent_spec.loader.exec_module(parent_module)
    parent_a = parent_module.parent_id("t", "kb", "A", "task", "same parent")
    parent_b = parent_module.parent_id("t", "kb", "B", "task", "same parent")
    assert parent_a != parent_b
    observations["parent_source_identity"] = {"equal_text_distinct_documents_have_distinct_ids": True}
    verdict_ns = {"SufficiencyVerdict": NS}
    extract("ragflow/rag/advanced_rag/harness/sufficiency.py", {"claim_verdict"}, verdict_ns)
    claim = NS(claim_id="c", required=True, is_verified=True, confidence=1.0,
               agent_result=NS(evidence_ids=[0]))
    verdict = verdict_ns["claim_verdict"]([claim], [])
    assert verdict.status != "SUFFICIENT"
    observations["agentic_self_report_is_not_verification"] = {"status": verdict.status}
    observations["empty_metadata_scope"] = await retrieval_empty_metadata_probe()
    # A non-empty Message and normal EOF still require workflow_finished.
    message = {"event": "message", "data": {"content": "synthetic partial answer"}}
    reference = {"chunks": [{"doc_id": "allowed-a", "content": "synthetic evidence"}]}
    message_end = {"event": "message_end", "data": {"reference": reference}}
    observations["workflow_missing_terminal"] = workflow_event_probe([message, message_end])
    assert observations["workflow_missing_terminal"]["error_code"] == "RUN_INTERRUPTED"
    observations["workflow_failed_status_monotonic"] = workflow_event_probe([
        message, {"event": "message_end", "data": {"reference": reference, "status": "failed"}},
        {"event": "workflow_finished", "data": {"usage": {}}}])
    assert observations["workflow_failed_status_monotonic"]["status"] == "failed"
    observations["workflow_node_error_recovery"] = workflow_event_probe([
        message, message_end, {"event": "node_finished", "data": {"error": "synthetic node failure"}},
        {"event": "message_end", "data": {"status": "completed"}},
        {"event": "workflow_finished", "data": {}}])
    assert observations["workflow_node_error_recovery"]["status"] == "completed"
    assert observations["workflow_node_error_recovery"]["node_errors"] == ["synthetic node failure"]
    observations["workflow_abstain_status_is_explicit"] = workflow_event_probe([
        {"event": "message", "data": {"content": "当前检索结果中没有找到可靠依据"}},
        {"event": "message_end", "data": {"status": "no_reliable_evidence"}},
        {"event": "workflow_finished", "data": {}}])
    assert observations["workflow_abstain_status_is_explicit"]["status"] == "no_reliable_evidence"
    observations["workflow_unselected_reference_does_not_infer_state"] = workflow_event_probe([
        message, {"event": "message_end", "data": {"reference": {"chunks": [
            {"doc_id": "outside-g", "content": "synthetic outside"}]}, "status": "completed"}},
        {"event": "workflow_finished", "data": {}}])
    assert observations["workflow_unselected_reference_does_not_infer_state"]["status"] == "completed"
    acl_ns = {"AclDecision": NS}
    extract("enterprise/gateway/acl/policy.py", {"_deny", "evaluate_document_acl"}, acl_ns)
    acl = acl_ns["evaluate_document_acl"](NS(tenant_id="t", is_active=True,
        group_ids=("denied",), security_level=0), NS(tenant_id="t", business_status="active",
        security_level=9, deny_group_ids=("denied",), allow_group_ids=("other",)))
    assert acl.allowed
    observations["tenant_open_acl"] = {"denied_group_and_low_security_level_allowed": acl.allowed, "rule": acl.rule}
    from enterprise.gateway.query.citation_select import sanitize_citation_markers
    original = "检查 [L1] 端子，使用 arr[0]，参见 [手册](https://example.invalid/manual)。"
    changed = sanitize_citation_markers(original)
    assert changed == original
    observations["noncitation_text_preserved"] = {"input": original, "output": changed}
    from enterprise.gateway.query.answer_split import split_assistant_output, public_reasoning
    raw = public_reasoning(split_assistant_output("<think>synthetic private reasoning</think>answer").reasoning)
    assert raw == "正在处理请求。"
    observations["public_reasoning_is_safe_stage"] = {"synthetic_reasoning_preserved": False}
    from enterprise.gateway.query.preference_rules import extract_preferences, preference_text
    assert extract_preferences("设备额定电压是220V") == {}
    assert extract_preferences("以后请简短回答") == {"detail": "brief"}
    assert preference_text({"detail": "untrusted instructions"}) == ""
    observations["preference_candidates_are_finite"] = {"technical_facts_accepted": False}
    from enterprise.gateway.query.answer_split import safe_execution_reasoning
    replay_ns = {"safe_execution_reasoning": safe_execution_reasoning,
                 "_sse": lambda event, body: (event, body), "v2_store": NS(public_status=lambda x: x)}
    extract("enterprise/gateway/query/v2_router.py", {"_result_events"}, replay_ns)
    replay = {"conversationId": "synthetic", "clientMessageId": "synthetic", "runId": "synthetic",
              "messageId": "synthetic", "replayed": True, "status": "failed", "answer": "partial",
              "citations": [], "_error": {"body": {"code": "RUN_INTERRUPTED"}},
              "_streamDeltas": [{"event": "reasoning.delta", "content": "synthetic private reasoning"}]}
    events = [event async for event in replay_ns["_result_events"](replay)]
    assert [name for name, _ in events] == ["run.started", "answer.delta", "run.failed"]
    assert "synthetic private reasoning" not in str(events)
    observations["failed_replay_has_one_error_terminal"] = {"answer_completed_emitted": False}
    print(json.dumps({"kind": "source-level synthetic audit observations", "observations": observations}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
