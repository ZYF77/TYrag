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
import importlib
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


async def workflow_stream_probe(frames):
    split = importlib.import_module("enterprise.gateway.query.answer_split")
    cite = importlib.import_module("enterprise.gateway.query.citation_select")
    writes, failures = [], []
    class APIError(Exception):
        def __init__(self, message, status_code=None, *args):
            super().__init__(message)
            self.status_code = status_code
    class FormalError(Exception):
        def __init__(self, code, status_code, message):
            self.code, self.status_code, self.message = code, status_code, message
    async def stream(**kwargs):
        for frame in frames:
            yield frame
    async def scope(*args):
        return NS(is_empty=False), {"allowed-a": NS()}
    async def question(*args, **kwargs):
        return "synthetic question", None, []
    async def write(db, fn, **kwargs):
        writes.append({"operation": fn, **kwargs})
    async def project(db, citations, *args):
        return citations
    async def fail(*args, **kwargs):
        failures.append(kwargs["code"])
    ns = {"asyncio": asyncio, "uuid": uuid, "json": json, "perf_counter": perf_counter,
          "logger": logging.getLogger("audit"), "_VALID_STATUSES": {"completed", "no_reliable_evidence", "failed"},
          "_MAX_WORKFLOW_REASONING_CHARS": 12000, "_WF_TOOL_SSE_EVENTS": set(),
          "_workflow_configuration": lambda: ("synthetic-agent", "v1"),
          "_workflow_client": lambda: NS(stream=stream), "_workflow_inputs": lambda *a: {},
          "_workflow_start_trace": noop, "_workflow_record_run_started": noop,
          "_workflow_merge_upstream_diagnostics": noop, "_workflow_record_canvas_node_events": noop,
          "_workflow_record_canvas_tool_events": noop, "_workflow_record_tools_summary": noop,
          "_workflow_finish_trace": lambda *a, **k: {}, "_workflow_diagnostics_return_trace": lambda: False,
          "fetch_user_memory_text": noop_async, "schedule_memory_candidate": noop,
          "cleanup_ragflow_files": noop_async}
    # Use the real citation selector: no marker means no selected references.
    ext = {"select_cited_chunk_refs": cite.select_cited_chunk_refs, "_FormalQueryError": FormalError}
    extract("enterprise/gateway/query/v2_router.py", {"_external_citations"}, ext)
    ns["v2"] = NS(StreamThinkSplitter=split.StreamThinkSplitter, finalize_streamed_output=split.finalize_streamed_output,
                  sanitize_citation_markers=cite.sanitize_citation_markers,
                  _contains_tool_protocol_artifact=lambda value: False,
                  _mapped_llm_provider_failure=lambda value: None,
                  _retrieval_question=question, _context_scope=scope,
                  _external_citations=ext["_external_citations"], _project_citations=project,
                  _gw_write=write, _save_failed_run=fail,
                  _sse=lambda event, data: {"event": event, "data": data},
                  record_timed_event=noop, NO_RELIABLE_EVIDENCE_ANSWER=cite.ABSTAIN_PHRASE,
                  _FormalQueryError=FormalError, RAGFlowAPIError=APIError, httpx=NS(HTTPError=APIError),
                  v2_store=NS(add_message="add_message", set_workflow_session="set_session",
                              complete_message_run="complete_run", public_status=lambda s: s))
    extract("enterprise/gateway/query/workflow_router.py", {
        "_workflow_stream", "_workflow_frame", "_workflow_chunks", "_workflow_reference",
        "_workflow_status_from", "_workflow_attachment_ids", "_workflow_evidence_present",
        "_public_request_id", "_workflow_record_stream_first_packets"}, ns)
    req = NS(clientMessageId="synthetic-id", internetEnabled=False)
    events = [event async for event in ns["_workflow_stream"](
        None, NS(tenant_id="t", business_user_id="u"), {"conversation_id": "c"},
        req, req, "synthetic question", {"run_id": "r", "assistant_message_id": "m"}, [], NS())]
    assert not failures, failures
    end = events[-1]
    assert end["event"] == "answer.completed"
    return {"terminal_event": end["event"], "status": end["data"]["status"],
            "citations": len(end["data"]["citations"])}


async def main():
    observations = {}
    statements = []
    async def sql(conn, statement, params):
        statements.append(statement)
        return NS(rowcount=0)  # running lease has NOT expired
    async def fetch(conn, statement, params):
        return {"assistant_message_id": "synthetic-message"}
    async def get_run(*args, **kwargs):
        return {"status": "running"}
    ns = {"utc_now": lambda: "2026-09-20T00:00:00Z", "json": json,
          "exec_sql": sql, "fetchone": fetch, "get_message_run": get_run}
    extract("enterprise/gateway/query/v2_store.py", {"mark_expired_run_interrupted"}, ns)
    run = await ns["mark_expired_run_interrupted"](None, conversation_id="c",
        tenant_id="t", business_user_id="u", client_message_id="client")
    assert run["status"] == "running" and any("INSERT INTO ext_v2_message" in s for s in statements)
    observations["pending_retry_inserts_failed_message"] = {
        "lease_expiry_update_rows": 0, "run_status": run["status"], "failed_message_insert_attempted": True}
    observations["empty_metadata_scope"] = await retrieval_empty_metadata_probe()
    # Simulate a well-framed HTTP stream ending normally after an intermediate
    # Message, but without the required workflow terminal event.
    message = {"event": "message", "data": {"content": "synthetic partial answer"}}
    reference = {"chunks": [{"doc_id": "allowed-a", "content": "synthetic evidence"}]}
    message_end = {"event": "message_end", "data": {"reference": reference}}
    observations["missing_workflow_terminal"] = await workflow_stream_probe([message, message_end])
    assert observations["missing_workflow_terminal"]["status"] == "completed"
    observations["message_status_overwritten"] = await workflow_stream_probe([
        message, {"event": "message_end", "data": {"reference": reference, "status": "failed"}},
        {"event": "workflow_finished", "data": {"usage": {}}}])
    assert observations["message_status_overwritten"]["status"] == "completed"
    observations["canvas_error_ignored"] = await workflow_stream_probe([
        message, message_end, {"event": "node_finished", "data": {"error": "synthetic node failure"}}])
    assert observations["canvas_error_ignored"]["status"] == "completed"
    observations["abstain_text_with_retrieval"] = await workflow_stream_probe([
        {"event": "message", "data": {"content": "当前检索结果中没有找到可靠依据"}},
        message_end, {"event": "workflow_finished", "data": {}}])
    assert observations["abstain_text_with_retrieval"]["status"] == "completed"
    observations["unselected_out_of_scope_reference"] = await workflow_stream_probe([
        message, {"event": "message_end", "data": {"reference": {"chunks": [
            {"doc_id": "outside-g", "content": "synthetic outside"}]}}},
        {"event": "workflow_finished", "data": {}}])
    assert observations["unselected_out_of_scope_reference"]["status"] == "completed"
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
    assert "[L1]" not in changed and "[手册]" not in changed and "arr[ID:0]" in changed
    observations["noncitation_text_corruption"] = {"input": original, "output": changed}
    from enterprise.gateway.query.answer_split import split_assistant_output, public_reasoning
    raw = public_reasoning(split_assistant_output("<think>synthetic private reasoning</think>answer").reasoning)
    assert raw == "synthetic private reasoning"
    observations["public_reasoning_is_unredacted"] = {"synthetic_reasoning_preserved": True}
    print(json.dumps({"kind": "source-level synthetic audit observations", "observations": observations}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
