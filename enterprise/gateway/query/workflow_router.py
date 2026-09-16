"""Second, opt-in Gateway entry point for the RAGFlow Agent Workflow runtime.

The existing ``/enterprise/api/v1/conversations/.../messages`` route remains the
production Chat baseline. This router reuses its ownership, scope, attachment,
citation, status and Memory helpers, but sends the actual turn to a configured
RAGFlow canvas through ``/api/v1/agents/chat/completions``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from time import perf_counter
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config, require_ragflow_api_key
from enterprise.gateway.query import v2_router as v2
from enterprise.gateway.query.attachment_context import (
    cleanup_ragflow_files,
)
from enterprise.gateway.query.user_memory import (
    fetch_user_memory_text,
    schedule_memory_candidate,
)
from enterprise.gateway.query.workflow_client import (
    RAGFlowAgentClient,
    RAGFlowAgentStub,
)

logger = logging.getLogger(__name__)

# Internal Console test surface; keep the frozen external OpenAPI unchanged.
router = APIRouter(
    prefix="/enterprise/api/v1/workflow",
    tags=["workflow"],
    include_in_schema=False,
)

_workflow_client_instance: RAGFlowAgentClient | RAGFlowAgentStub | None = None
_VALID_STATUSES = {"completed", "no_reliable_evidence", "failed"}
_MAX_WORKFLOW_REASONING_CHARS = 12_000


def _workflow_client():
    global _workflow_client_instance
    if _workflow_client_instance is None:
        if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
            _workflow_client_instance = RAGFlowAgentStub()
        else:
            _workflow_client_instance = RAGFlowAgentClient(
                api_key=require_ragflow_api_key()
            )
    return _workflow_client_instance


def set_workflow_client_for_tests(client) -> None:
    global _workflow_client_instance
    _workflow_client_instance = client


def _workflow_runtime():
    """Effective hot-reloadable snapshot (not frozen boot-only config)."""
    return config.runtime_settings()


def _workflow_configuration() -> tuple[str, str] | None:
    """Return (agent_id, version) when Workflow is fully configured.

    Reads the runtime snapshot (Console / env backfill). enabled=true with
    missing agentId/version returns None so callers emit WORKFLOW_NOT_CONFIGURED
    and must not silently fall back to Chat.
    """
    snap = _workflow_runtime()
    agent_id = str(getattr(snap, "workflow_agent_id", "") or "").strip()
    version = str(getattr(snap, "workflow_version", "") or "").strip()
    if not bool(getattr(snap, "workflow_enabled", False)) or not agent_id or not version:
        return None
    return agent_id, version


def _internal_request(req: v2.CreateMessageRequest) -> v2.CreateMessageRequest:
    """Namespace idempotency keys so Chat and Workflow never replay each other."""
    return req.model_copy(
        update={"clientMessageId": f"workflow:{req.clientMessageId}"}
    )


def _public_request_id(req: v2.CreateMessageRequest) -> str:
    return req.clientMessageId.removeprefix("workflow:")


def _workflow_frame(payload: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Extract ``event``, event data and session id from Agent API envelopes."""
    event = str(payload.get("event") or "")
    session_id = payload.get("session_id") or payload.get("sessionId")
    data = payload.get("data")
    if not event and isinstance(data, dict) and data.get("event"):
        event = str(data.get("event") or "")
        session_id = session_id or data.get("session_id") or data.get("sessionId")
        inner = data.get("data")
        if isinstance(inner, dict):
            session_id = session_id or inner.get("session_id") or inner.get("sessionId")
        data = inner
    if not isinstance(data, dict):
        data = {}
    return event, data, str(session_id) if session_id else None


def _workflow_chunks(reference: Any) -> list[dict]:
    """Normalize Canvas references to the chunk shape used by citation code."""
    if isinstance(reference, dict):
        if "chunks" in reference:
            raw = reference.get("chunks", [])
        elif "reference" in reference:
            raw = reference.get("reference", [])
        elif any(
            key in reference
            for key in ("id", "chunk_id", "doc_id", "document_id", "content")
        ):
            raw = [reference]
        else:
            # Older Agent API responses may expose the ordinal chunk map
            # directly instead of wrapping it under ``chunks``.
            raw = reference
    else:
        raw = reference
    items: list[tuple[Any, Any]]
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(None, item) for item in raw]
    else:
        items = []
    result: list[dict] = []
    for key, item in items:
        if not isinstance(item, dict):
            continue
        chunk = dict(item)
        # Canvas keys are citation IDs, not list offsets. Preserve them before
        # normalizing the reference map into a list for the public adapter.
        if key is not None:
            chunk["citation_id"] = str(key)
        if key is not None and not chunk.get("id") and not chunk.get("chunk_id"):
            chunk["id"] = str(key)
        if "document_id" not in chunk and chunk.get("doc_id"):
            chunk["document_id"] = chunk["doc_id"]
        if "document_name" not in chunk and chunk.get("docnm_kwd"):
            chunk["document_name"] = chunk["docnm_kwd"]
        if "dataset_id" not in chunk and chunk.get("kb_id"):
            chunk["dataset_id"] = chunk["kb_id"]
        if "content" not in chunk and chunk.get("content_with_weight"):
            chunk["content"] = chunk["content_with_weight"]
        result.append(chunk)
    return result


def _workflow_reference(reference: Any) -> dict:
    if isinstance(reference, list):
        return {"chunks": _workflow_chunks(reference), "doc_aggs": []}
    if not isinstance(reference, dict):
        return {"chunks": [], "doc_aggs": []}
    return {
        "chunks": _workflow_chunks(reference),
        "doc_aggs": reference.get("doc_aggs", [])
        if isinstance(reference.get("doc_aggs", []), list)
        else [],
    }


def _workflow_attachment_ids(pending: list[v2.PendingAttachment]) -> set[str]:
    return {
        str((item.ragflow_file or {}).get("id") or "")
        for item in pending
        if (item.ragflow_file or {}).get("id")
    }


def _workflow_evidence_present(
    chunks: list[dict],
    citations: list[dict],
    pending: list[v2.PendingAttachment],
) -> bool:
    """Return whether a completed Workflow answer has usable evidence.

    Document/web citations are already authorization-checked by
    ``_external_citations``. Temporary attachment chunks intentionally do not
    become public citations, so an attachment reference is accepted as a
    second evidence form for attachment-only questions.

    Chat-parity (v1.5): production Chat does not post-filter a completed
    answer merely because citation markers were omitted. Treat in-scope
    retrieval chunks as usable evidence so Direct answers that omit ``[ID:n]``
    are not false-killed by this Gateway gate.
    """
    if citations:
        return True
    attachment_ids = _workflow_attachment_ids(pending)
    if any(
        str(chunk.get("document_id") or chunk.get("doc_id") or "")
        in attachment_ids
        for chunk in chunks
        if isinstance(chunk, dict)
    ):
        return True
    return any(
        isinstance(chunk, dict)
        and (
            str(
                chunk.get("content")
                or chunk.get("content_with_weight")
                or ""
            ).strip()
            or str(chunk.get("document_id") or chunk.get("doc_id") or "").strip()
        )
        for chunk in chunks
    )


def _workflow_start_trace(
    run: dict,
    request: Request,
    question: str,
    req: v2.CreateMessageRequest,
    *,
    stream: bool,
) -> None:
    if not getattr(config, "rag_diagnostics_enabled", False):
        return
    try:
        run["_diagnostics"] = v2.start_trace(
            run["run_id"],
            query=question,
            reasoning_mode=req.reasoningMode,
            stream=stream,
            request_started=getattr(
                request.state, "gateway_request_started", None
            ),
        )
    except Exception:
        run["_diagnostics"] = None


def _workflow_finish_trace(
    run: dict,
    request: Request,
    *,
    outcome: str,
) -> dict:
    v2._record_http_response_timing(run.get("_diagnostics"), request)
    return v2.finish_trace(run.get("_diagnostics"), outcome=outcome)


def _workflow_diagnostics_return_trace() -> bool:
    """Ask Agent API for upstream ``_diagnostics`` when Gateway diagnostics are on."""
    return bool(getattr(config, "rag_diagnostics_enabled", False))


def _workflow_record_run_started(run: dict, request: Request) -> None:
    """Record Chat-aligned ``run_started`` (SSE emit or JSON-path milestone)."""
    request_started = getattr(request.state, "gateway_request_started", None)
    started = (
        request_started if request_started is not None else perf_counter()
    )
    v2.record_timed_event(
        run.get("_diagnostics"),
        "run_started",
        started,
        {
            "source": "gateway",
            "stage": "run_started_emit",
            "status": "success",
        },
    )


def _workflow_merge_upstream_diagnostics(
    run: dict, *candidates: Any
) -> None:
    """Merge Agent/Canvas ``_diagnostics`` when present (caps/redaction in diagnostics.py)."""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        upstream = candidate.get("_diagnostics")
        if isinstance(upstream, dict):
            v2.merge_upstream(run.get("_diagnostics"), upstream)


def _workflow_record_stream_first_packets(
    run: dict,
    *,
    upstream_started: float,
    data: dict[str, Any],
    content: str,
    first_flags: dict[str, bool],
    splitter: Any,
) -> list[tuple[str, str]]:
    """Derive Chat-isomorphic first-packet events from RF message frames.

    Mutates ``first_flags`` keys: stream_output / reasoning / answer.
    Returns splitter pieces for the caller to accumulate / emit.
    """
    if (
        not first_flags.get("stream_output")
        and (
            content
            or data.get("start_to_think")
            or data.get("end_to_think")
        )
    ):
        v2.record_timed_event(
            run.get("_diagnostics"),
            "stream_first_token",
            upstream_started,
            {
                "source": "gateway",
                "stage": "stream_first_token",
                "status": "success",
            },
        )
        first_flags["stream_output"] = True

    pieces = splitter.feed(
        content,
        start_to_think=bool(data.get("start_to_think")),
        end_to_think=bool(data.get("end_to_think")),
    )
    for kind, chunk in pieces:
        if not chunk:
            continue
        if kind == "reasoning" and not first_flags.get("reasoning"):
            v2.record_timed_event(
                run.get("_diagnostics"),
                "stream_first_reasoning",
                upstream_started,
                {
                    "source": "gateway",
                    "stage": "stream_first_reasoning",
                    "status": "success",
                },
            )
            first_flags["reasoning"] = True
        elif kind == "answer" and not first_flags.get("answer"):
            v2.record_timed_event(
                run.get("_diagnostics"),
                "stream_first_answer",
                upstream_started,
                {
                    "source": "gateway",
                    "stage": "stream_first_answer",
                    "status": "success",
                },
            )
            first_flags["answer"] = True
    return pieces



_WF_NODE_STR_LIMIT = 256
_WF_NODE_EVENT_MAP = {
    "node_started": "wf_node_started",
    "node_finished": "wf_node_finished",
}


def _workflow_canvas_node_summary(
    data: dict[str, Any], *, event: str
) -> dict[str, Any]:
    """Panel-safe node fields only — never store inputs/outputs payloads."""
    summary: dict[str, Any] = {
        "source": "gateway",
        "componentId": data.get("component_id"),
        "componentName": data.get("component_name"),
        "componentType": data.get("component_type"),
    }
    error = data.get("error")
    if error not in (None, ""):
        summary["error"] = str(error)[:_WF_NODE_STR_LIMIT]
        summary["status"] = "error"
    elif event == "node_started":
        summary["status"] = "running"
    else:
        summary["status"] = "success"
    elapsed = data.get("elapsed_time")
    if isinstance(elapsed, (int, float)) and elapsed >= 0:
        summary["elapsedSec"] = round(float(elapsed), 6)
        summary["durationMs"] = round(float(elapsed) * 1000.0, 3)
    # Drop empties so Console timeline stays compact.
    return {
        key: (
            value[:_WF_NODE_STR_LIMIT]
            if isinstance(value, str) and key != "status"
            else value
        )
        for key, value in summary.items()
        if value is not None and value != ""
    }


def _workflow_record_canvas_node_events(
    run: dict,
    *,
    event: str,
    data: dict[str, Any],
) -> None:
    """Record ``wf_node_started`` / ``wf_node_finished`` from RF Canvas SSE frames."""
    mapped = _WF_NODE_EVENT_MAP.get(str(event or ""))
    if not mapped or not isinstance(data, dict):
        return
    payload = _workflow_canvas_node_summary(data, event=str(event or ""))
    payload["stage"] = mapped
    # Explicitly refuse giant Canvas payloads even if callers pass them through.
    for blocked in ("inputs", "outputs", "thoughts", "content", "answer"):
        payload.pop(blocked, None)
    v2.record_event(run.get("_diagnostics"), mapped, payload)


# RF canvas tool component_type values (agent/tools). Retrieval is a first-class
# pipeline node on the v1.7 canvas, so it is intentionally excluded here — it already
# appears as wf_node_*; counting it as a tool call would hide empty-tool production runs.
_WF_TOOL_COMPONENT_TYPES = frozenset(
    {
        "AkShare",
        "ArXiv",
        "BGPT",
        "CodeExec",
        "Crawler",
        "DeepL",
        "DuckDuckGo",
        "Email",
        "ExeSQL",
        "GitHub",
        "Google",
        "GoogleScholar",
        "Jin10",
        "KeenableSearch",
        "PubMed",
        "QWeather",
        "QueritSearch",
        "SearXNG",
        "TavilyExtract",
        "TavilySearch",
        "TuShare",
        "WenCai",
        "Wikipedia",
        "YahooFinance",
    }
)
_WF_TOOL_SSE_EVENTS = frozenset({"tool_call", "mcp_call", "tool_use"})
_WF_TOOL_STR_LIMIT = _WF_NODE_STR_LIMIT


def _workflow_tool_agg(run: dict) -> dict[str, dict[str, Any]]:
    agg = run.get("_wf_tool_agg")
    if not isinstance(agg, dict):
        agg = {}
        run["_wf_tool_agg"] = agg
    return agg


def _workflow_truncate_tool_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:_WF_TOOL_STR_LIMIT]


def _workflow_tool_duration_ms(data: dict[str, Any]) -> float | None:
    for key in ("durationMs", "duration_ms", "toolDurationMs", "tool_duration_ms"):
        raw = data.get(key)
        if isinstance(raw, (int, float)) and raw >= 0:
            return round(float(raw), 3)
    elapsed = data.get("elapsed_time")
    if isinstance(elapsed, (int, float)) and elapsed >= 0:
        return round(float(elapsed) * 1000.0, 3)
    return None


def _workflow_tool_status(data: dict[str, Any]) -> tuple[str, str | None]:
    error = data.get("error")
    if error not in (None, ""):
        return "error", _workflow_truncate_tool_str(error)
    status = str(data.get("status") or "").lower()
    if status in {"error", "failed", "failure"}:
        return "error", _workflow_truncate_tool_str(data.get("error") or status)
    if status in {"ok", "success", "succeeded", "completed"}:
        return "success", None
    return "success", None


def _workflow_infer_tool_source(data: dict[str, Any], *, component_type: str) -> str:
    for key in ("toolSource", "tool_source", "callSource", "call_source"):
        raw = str(data.get(key) or "").strip().lower()
        if raw in {"tool", "mcp"}:
            return raw
    if data.get("mcp_id") or data.get("mcpId") or data.get("mcp"):
        return "mcp"
    ctype = component_type.lower()
    if "mcp" in ctype:
        return "mcp"
    src = str(data.get("source") or "").strip().lower()
    if src == "mcp":
        return "mcp"
    return "tool"


def _workflow_is_tool_component_type(component_type: str) -> bool:
    if not component_type:
        return False
    if component_type in _WF_TOOL_COMPONENT_TYPES:
        return True
    lowered = component_type.lower()
    if lowered in {t.lower() for t in _WF_TOOL_COMPONENT_TYPES}:
        return True
    if "mcp" in lowered:
        return True
    return False


def _workflow_extract_tool_calls(
    data: dict[str, Any], *, event: str
) -> list[dict[str, Any]]:
    """Return panel-safe tool/MCP call summaries (never args/results)."""
    if not isinstance(data, dict):
        return []
    calls: list[dict[str, Any]] = []
    component_type = str(
        data.get("component_type") or data.get("componentType") or ""
    ).strip()
    tool_name = (
        data.get("tool_name")
        or data.get("toolName")
        or data.get("function_name")
        or data.get("functionName")
    )
    sse_tool = str(event or "") in _WF_TOOL_SSE_EVENTS
    if sse_tool and not tool_name:
        tool_name = data.get("name")
    tool_id = data.get("tool_id") or data.get("toolId") or data.get("component_id")
    explicit_tool = bool(tool_name) or bool(
        data.get("mcp_id") or data.get("mcpId") or data.get("mcp")
    )
    type_tool = _workflow_is_tool_component_type(component_type)

    # Prefer finished/call frames; ignore bare node_started unless explicit tool SSE.
    if str(event or "") == "node_started" and not (sse_tool or explicit_tool):
        return []

    if type_tool or explicit_tool or sse_tool:
        name = _workflow_truncate_tool_str(
            tool_name or component_type or data.get("component_name") or "unknown_tool"
        )
        if name:
            status, error = _workflow_tool_status(data)
            entry: dict[str, Any] = {
                "name": name,
                "toolSource": _workflow_infer_tool_source(
                    data, component_type=component_type
                ),
                "count": 1,
                "status": status,
            }
            tid = _workflow_truncate_tool_str(tool_id)
            if tid:
                entry["toolId"] = tid
            if component_type:
                entry["componentType"] = _workflow_truncate_tool_str(component_type)
            duration = _workflow_tool_duration_ms(data)
            if duration is not None:
                entry["durationMs"] = duration
            if error:
                entry["error"] = error
            calls.append(entry)

    # Shallow scan of RF tool_use_callback-shaped trace rows (name/latency only).
    for key in ("trace", "tool_calls", "toolCalls", "tools"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:128]:
            if not isinstance(row, dict):
                continue
            nested = row.get("trace")
            candidates = nested if isinstance(nested, list) else [row]
            for item in candidates[:128]:
                if not isinstance(item, dict):
                    continue
                tname = item.get("tool_name") or item.get("toolName")
                if not tname:
                    continue
                status, error = _workflow_tool_status(item)
                entry = {
                    "name": _workflow_truncate_tool_str(tname) or "unknown_tool",
                    "toolSource": _workflow_infer_tool_source(
                        item, component_type=str(item.get("component_type") or "")
                    ),
                    "count": 1,
                    "status": status,
                }
                tid = _workflow_truncate_tool_str(
                    item.get("tool_id")
                    or item.get("toolId")
                    or row.get("component_id")
                    or data.get("component_id")
                )
                if tid:
                    entry["toolId"] = tid
                duration = _workflow_tool_duration_ms(item)
                if duration is not None:
                    entry["durationMs"] = duration
                if error:
                    entry["error"] = error
                calls.append(entry)
    return calls


def _workflow_accumulate_tool_call(run: dict, call: dict[str, Any]) -> None:
    agg = _workflow_tool_agg(run)
    name = str(call.get("name") or "unknown_tool")
    tool_source = str(call.get("toolSource") or "tool")
    key = f"{tool_source}:{name}"
    bucket = agg.get(key)
    if not isinstance(bucket, dict):
        bucket = {
            "name": name,
            "toolSource": tool_source,
            "count": 0,
            "durationMs": 0.0,
            "ok": 0,
            "error": 0,
        }
        tid = call.get("toolId")
        if tid:
            bucket["toolId"] = tid
        agg[key] = bucket
    bucket["count"] = int(bucket.get("count") or 0) + int(call.get("count") or 1)
    duration = call.get("durationMs")
    if isinstance(duration, (int, float)) and duration >= 0:
        bucket["durationMs"] = round(
            float(bucket.get("durationMs") or 0.0) + float(duration), 3
        )
    if str(call.get("status") or "") == "error":
        bucket["error"] = int(bucket.get("error") or 0) + 1
    else:
        bucket["ok"] = int(bucket.get("ok") or 0) + 1


def _workflow_record_canvas_tool_events(
    run: dict,
    *,
    event: str,
    data: dict[str, Any],
) -> None:
    """Record per-call ``wf_tool_call`` from tool-like Canvas SSE frames.

    Default: never store tool args/results (opt-in later). Uses the shared
    diagnostics scrubber via ``record_event``.
    """
    if not isinstance(data, dict):
        return
    for call in _workflow_extract_tool_calls(data, event=str(event or "")):
        payload = {
            "source": "gateway",
            "stage": "wf_tool_call",
            "name": call["name"],
            "toolSource": call["toolSource"],
            "count": int(call.get("count") or 1),
            "status": call.get("status") or "success",
        }
        if call.get("toolId"):
            payload["toolId"] = call["toolId"]
        if call.get("componentType"):
            payload["componentType"] = call["componentType"]
        if isinstance(call.get("durationMs"), (int, float)):
            payload["durationMs"] = call["durationMs"]
        if call.get("error"):
            payload["error"] = call["error"]
        for blocked in (
            "inputs",
            "outputs",
            "thoughts",
            "content",
            "answer",
            "arguments",
            "args",
            "params",
            "result",
            "toolArgs",
            "toolResult",
        ):
            payload.pop(blocked, None)
        v2.record_event(run.get("_diagnostics"), "wf_tool_call", payload)
        _workflow_accumulate_tool_call(run, call)


def _workflow_record_tools_summary(run: dict) -> None:
    """Emit one aggregated ``wf_tools_summary`` when any tool/MCP calls were seen."""
    agg = run.get("_wf_tool_agg")
    if not isinstance(agg, dict) or not agg:
        return
    tools = []
    total = 0
    for bucket in agg.values():
        if not isinstance(bucket, dict):
            continue
        item = {
            "name": _workflow_truncate_tool_str(bucket.get("name")) or "unknown_tool",
            "toolSource": str(bucket.get("toolSource") or "tool"),
            "count": int(bucket.get("count") or 0),
            "durationMs": round(float(bucket.get("durationMs") or 0.0), 3),
            "ok": int(bucket.get("ok") or 0),
            "error": int(bucket.get("error") or 0),
        }
        if bucket.get("toolId"):
            item["toolId"] = _workflow_truncate_tool_str(bucket.get("toolId"))
        tools.append(item)
        total += item["count"]
    if total <= 0:
        return
    tools.sort(key=lambda row: (str(row.get("toolSource") or ""), str(row.get("name") or "")))
    v2.record_event(
        run.get("_diagnostics"),
        "wf_tools_summary",
        {
            "source": "gateway",
            "stage": "wf_tools_summary",
            "totalCount": total,
            "tools": tools[:128],
        },
    )


def _soft_business_context(conversation: dict) -> dict:
    """Merge durable conversation identity into soft business_context.

    Chat always surfaces equipment_id via ``allowed_identifiers`` /
    ``scope_identifiers`` plus business_context. Workflow historically only
    forwarded ``business_context_json``; fill gaps from conversation columns
    so Direct Soft Device Gate sees the same soft device/model hints. Never
    mutates authorized doc_ids.
    """
    context = dict(v2.v2_store.business_context_from_row(conversation) or {})
    for key in ("equipment_id", "fixed_asset_no", "fault_code"):
        if key in context:
            continue
        value = v2._conversation_value(conversation, key)
        if value:
            context[key] = value
    return v2.v2_store.normalize_business_context(context)


def _gateway_soft_context_line(conversation: dict) -> str:
    """Chat-parity soft device/model hints for Begin@gateway_context."""
    business = _soft_business_context(conversation)
    identifiers: list[str] = []
    for key in ("equipment_id", "fixed_asset_no"):
        value = business.get(key) or v2._conversation_value(conversation, key)
        if value and value not in identifiers:
            identifiers.append(str(value))
    for value in conversation.get("_turn_entity_ids") or []:
        text = str(value or "").strip()
        if text and text not in identifiers:
            identifiers.append(text)
    for value in v2.v2_store.devices_from_row(conversation) or []:
        text = str(value or "").strip()
        if text and text not in identifiers:
            identifiers.append(text)
    parts: list[str] = []
    if identifiers:
        parts.append("scope_identifiers=" + ",".join(identifiers))
    for key in ("model", "equipment_type", "manufacturer"):
        if business.get(key):
            parts.append(f"{key}={business[key]}")
    if not parts:
        return ""
    return (
        "Chat-parity soft business hints (ranking/disambiguation only; "
        "do not shrink authorized_doc_ids): " + "; ".join(parts)
    )


def _workflow_inputs(
    conversation: dict,
    scope,
    user_memory: str,
    request: v2.CreateMessageRequest,
    version: str,
) -> dict[str, Any]:
    """Values consumed by the checked-in Begin/LLM/Retrieval workflow DSL."""
    business_context = _soft_business_context(conversation)
    gateway_context = _gateway_soft_context_line(conversation)
    return {
        # RAGFlow Begin inputs use the public ``{"type", "value"}`` form.
        # In particular, an unwrapped object would be interpreted as an input
        # descriptor and its business fields would be lost by UserFillUp.
        "authorized_dataset_ids": {
            "type": "array",
            "value": list(scope.dataset_ids),
        },
        "authorized_doc_ids": {
            "type": "array",
            "value": list(scope.document_ids),
        },
        "doc_scope_mode": {"type": "line", "value": "restrict"},
        "business_context": {"type": "object", "value": business_context},
        "gateway_context": {"type": "line", "value": gateway_context},
        "user_memory": {"type": "line", "value": user_memory},
        "internet_enabled": {
            "type": "boolean",
            "value": bool(request.internetEnabled),
        },
        "reasoning_mode": {"type": "line", "value": request.reasoningMode},
        "workflow_version": {"type": "line", "value": version},
    }


def _workflow_status_from(
    explicit: Any,
    answer: str,
) -> str:
    status = str(explicit or "").strip()
    if status in _VALID_STATUSES:
        return status
    if status:
        # A non-empty terminal value is part of the upstream contract. Treat
        # an unknown value as a failed run instead of deriving success from
        # answer text.
        return "failed"
    return "completed" if str(answer or "").strip() else "no_reliable_evidence"


async def _parse_request(
    request: Request,
    principal: UserPrincipal,
) -> tuple[v2.CreateMessageRequest, list[v2.PendingAttachment]]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        req, pending = await v2._parse_multipart_message(request, principal)
        request.state.inquiry_audit_body = v2._inquiry_audit_body(req, pending)
        return req, pending
    try:
        payload = await request.json()
    except Exception as exc:
        raise ValueError("Invalid JSON") from exc
    try:
        req = v2.CreateMessageRequest.model_validate(payload)
        v2._validate_reasoning_mode_access(principal, req.reasoningMode)
        return req, []
    except v2._ReasoningModeDenied as exc:
        raise exc
    except ValidationError as exc:
        raise ValueError("Invalid workflow message") from exc


async def _workflow_run_result(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: v2.CreateMessageRequest,
    internal_req: v2.CreateMessageRequest,
    question: str,
    run: dict,
    pending: list[v2.PendingAttachment],
    request: Request,
) -> tuple[dict | None, JSONResponse | None]:
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    config_values = _workflow_configuration()
    if config_values is None:
        return None, v2._error(
            503,
            "WORKFLOW_NOT_CONFIGURED",
            "Agent Workflow test runtime is not configured",
        )
    agent_id, version = config_values
    bound_session = str(conversation.get("workflow_session_id") or "").strip() or None
    bound_agent = str(conversation.get("workflow_agent_id") or "").strip()
    bound_version = str(conversation.get("workflow_version") or "").strip()
    if bound_session and (bound_agent != agent_id or bound_version != version):
        return None, v2._error(
            409,
            "WORKFLOW_VERSION_MISMATCH",
            "Conversation is bound to another Workflow version",
        )

    client = None
    _workflow_start_trace(run, request, question, req, stream=False)
    _workflow_record_run_started(run, request)
    try:
        question, client, observations = await v2._retrieval_question(
            db,
            principal,
            conversation,
            question,
            pending,
            diagnostics=run.get("_diagnostics"),
        )
        scope, docs_by_internal_id = await v2._context_scope(
            db, principal, conversation
        )
        answer = v2.NO_RELIABLE_EVIDENCE_ANSWER
        status = "no_reliable_evidence"
        reasoning = None
        chunks: list[dict] = []
        workflow_session_id = bound_session
        if not scope.is_empty:
            user_memory = await fetch_user_memory_text(
                principal, question, request_id=run["run_id"]
            )
            files = [
                dict(item.ragflow_file)
                for item in pending
                if isinstance(item.ragflow_file, dict)
            ]
            inputs = _workflow_inputs(
                conversation, scope, user_memory, req, version
            )
            workflow = _workflow_client()
            payload = await workflow.complete(
                agent_id=agent_id,
                question=question,
                session_id=workflow_session_id,
                user_id=principal.business_user_id,
                inputs=inputs,
                files=files,
                request_id=run["run_id"],
                return_trace=_workflow_diagnostics_return_trace(),
            )
            event, data, returned_session = _workflow_frame(payload)
            _workflow_merge_upstream_diagnostics(run, data, payload)
            workflow_session_id = returned_session or workflow_session_id
            raw_answer = str(data.get("content") or data.get("answer") or "")
            split = v2.split_assistant_output(raw_answer)
            if any(
                v2._contains_tool_protocol_artifact(value)
                for value in (raw_answer, split.answer, split.reasoning)
            ):
                raise v2._FormalQueryError(
                    "RAGFLOW_TOOL_PROTOCOL_INVALID",
                    502,
                    "Workflow returned an invalid tool result",
                )
            mapped = v2._mapped_llm_provider_failure(split.answer)
            if mapped is not None:
                code, status_code, message = mapped
                raise v2._FormalQueryError(code, status_code, message)
            answer = v2.sanitize_citation_markers(split.answer)
            status = _workflow_status_from(data.get("status"), answer)
            chunks = _workflow_chunks(data.get("reference") or data.get("references"))
            # Agent Workflow reasoning/think output is an internal model trace;
            # keep it out of the Gateway response and persisted message. The
            # split is still used to remove any wrappers before answer handling.
            reasoning = None
            if not event and not answer:
                raise v2.RAGFlowAPIError(
                    "RAGFlow Workflow returned an empty result", 502, run["run_id"]
                )
            if status != "completed":
                if status == "failed":
                    raise v2.RAGFlowAPIError(
                        "RAGFlow Workflow reported a failed run", 502, run["run_id"]
                    )
                answer = v2.NO_RELIABLE_EVIDENCE_ANSWER
                chunks = []
        citations = v2._external_citations(
            chunks,
            docs_by_internal_id,
            assistant_message_id,
            answer=answer,
            status=status,
            internet_enabled=req.internetEnabled,
            attachment_document_ids=_workflow_attachment_ids(pending),
        )
        if status == "completed" and not _workflow_evidence_present(
            chunks, citations, pending
        ):
            # The Agent must not turn an ungrounded final Message into a
            # successful business answer merely because it returned text.
            status = "no_reliable_evidence"
            answer = v2.NO_RELIABLE_EVIDENCE_ANSWER
            chunks = []
            citations = []
        public_citations = await v2._project_citations(
            db, citations, request, principal
        )
        await v2._gw_write(
            db,
            v2.v2_store.add_message,
            message_id=assistant_message_id,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            role="assistant",
            content=answer,
            status=status,
            citations=citations,
            reasoning=reasoning,
        )
        if workflow_session_id:
            await v2._gw_write(
                db,
                v2.v2_store.set_workflow_session,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                agent_id=agent_id,
                version=version,
                session_id=workflow_session_id,
            )
        result = {
            "conversationId": conversation["conversation_id"],
            "clientMessageId": _public_request_id(req),
            "runId": run["run_id"],
            "messageId": assistant_message_id,
            "answer": answer,
            "reasoning": reasoning,
            "status": status,
            "citations": public_citations,
            "workflowVersion": version,
            "replayed": False,
        }
        if pending:
            result["attachments"] = [
                v2._attachment_public_meta(item)
                for item in pending
                if item.attachment_id
            ]
        diagnostics = _workflow_finish_trace(run, request, outcome=status)
        if diagnostics:
            result["_diagnostics"] = diagnostics
        await v2._gw_write(
            db,
            v2.v2_store.complete_message_run,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            client_message_id=internal_req.clientMessageId,
            result=result,
            status="completed",
            assistant_message_id=assistant_message_id,
        )
        if status == "completed":
            schedule_memory_candidate(
                principal,
                chat_id=f"workflow:{agent_id}",
                session_id=workflow_session_id or run["run_id"],
                user_input=question,
                agent_response=answer,
                request_id=run["run_id"],
            )
        return result, None
    except asyncio.CancelledError:
        raise
    except v2._FormalQueryError as exc:
        return None, await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code=exc.code, status_code=exc.status_code, message=exc.message,
            request=request,
        )
    except v2.RAGFlowAPIError:
        return None, await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code="RAGFLOW_UNAVAILABLE", status_code=503,
            message="Query engine unavailable", request=request,
        )
    except Exception:
        logger.exception("workflow message run failed")
        return None, await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code="INTERNAL_ERROR", status_code=500,
            message="Workflow message run failed", request=request,
        )
    finally:
        await cleanup_ragflow_files(pending, client, db)


async def _workflow_stream(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: v2.CreateMessageRequest,
    internal_req: v2.CreateMessageRequest,
    question: str,
    run: dict,
    pending: list[v2.PendingAttachment],
    request: Request,
) -> AsyncIterator[str]:
    """Stream Canvas Message events through the stable Gateway SSE vocabulary."""
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    config_values = _workflow_configuration()
    if config_values is None:
        yield v2._sse("run.failed", {"code": "WORKFLOW_NOT_CONFIGURED", "message": "Agent Workflow test runtime is not configured"})
        return
    agent_id, version = config_values
    client = None
    accumulated = ""
    accumulated_reasoning = ""
    final_delta: str | None = None
    reference: dict = {"chunks": [], "doc_aggs": []}
    explicit_status: Any = None
    workflow_session_id = str(conversation.get("workflow_session_id") or "").strip() or None
    splitter = v2.StreamThinkSplitter()
    emitted_answer = ""
    first_flags = {
        "stream_output": False,
        "reasoning": False,
        "answer": False,
    }
    _workflow_start_trace(run, request, question, req, stream=True)
    _workflow_record_run_started(run, request)
    yield v2._sse(
        "run.started",
        {
            "conversationId": conversation["conversation_id"],
            "clientMessageId": _public_request_id(req),
            "runId": run["run_id"],
            "workflowVersion": version,
            "replayed": False,
        },
    )
    try:
        question, client, observations = await v2._retrieval_question(
            db,
            principal,
            conversation,
            question,
            pending,
            diagnostics=run.get("_diagnostics"),
        )
        scope, docs_by_internal_id = await v2._context_scope(db, principal, conversation)
        if not scope.is_empty:
            user_memory = await fetch_user_memory_text(
                principal, question, request_id=run["run_id"]
            )
            files = [
                dict(item.ragflow_file)
                for item in pending
                if isinstance(item.ragflow_file, dict)
            ]
            workflow = _workflow_client()
            upstream_started = perf_counter()
            async for payload in workflow.stream(
                agent_id=agent_id,
                question=question,
                session_id=workflow_session_id,
                user_id=principal.business_user_id,
                inputs=_workflow_inputs(conversation, scope, user_memory, req, version),
                files=files,
                request_id=run["run_id"],
                return_trace=_workflow_diagnostics_return_trace(),
            ):
                event, data, returned_session = _workflow_frame(payload)
                _workflow_merge_upstream_diagnostics(run, data, payload)
                workflow_session_id = returned_session or workflow_session_id
                if event in ("node_started", "node_finished"):
                    _workflow_record_canvas_node_events(
                        run, event=event, data=data
                    )
                    _workflow_record_canvas_tool_events(
                        run, event=event, data=data
                    )
                elif event in _WF_TOOL_SSE_EVENTS:
                    _workflow_record_canvas_tool_events(
                        run, event=event, data=data
                    )
                elif event == "message":
                    content = str(data.get("content") or data.get("answer") or "")
                    pieces = _workflow_record_stream_first_packets(
                        run,
                        upstream_started=upstream_started,
                        data=data,
                        content=content,
                        first_flags=first_flags,
                        splitter=splitter,
                    )
                    for kind, chunk in pieces:
                        if kind == "reasoning":
                            accumulated_reasoning = (
                                accumulated_reasoning + chunk
                            )[:_MAX_WORKFLOW_REASONING_CHARS]
                            # Do not expose model Chain-of-Thought through the
                            # public SSE stream. It is only retained in a
                            # bounded local buffer for safe tag stripping.
                            continue
                        else:
                            accumulated += chunk
                            emitted_answer += chunk
                            yield v2._sse(
                                "answer.delta",
                                {
                                    "conversationId": conversation["conversation_id"],
                                    "runId": run["run_id"],
                                    "content": chunk,
                                },
                            )
                elif event == "message_end":
                    reference = _workflow_reference(data.get("reference"))
                    if data.get("status") is not None:
                        explicit_status = data.get("status")
                elif event == "workflow_finished":
                    _workflow_record_tools_summary(run)
                    explicit_status = data.get("status")
                    if data.get("content"):
                        final_delta = str(data["content"])
                    if data.get("reference"):
                        reference = _workflow_reference(data["reference"])

        finalized = v2.finalize_streamed_output(
            accumulated, accumulated_reasoning, final_delta
        )
        raw_stream_outputs = (
            accumulated,
            accumulated_reasoning,
            final_delta or "",
            finalized.answer,
            finalized.reasoning,
        )
        if any(
            v2._contains_tool_protocol_artifact(value)
            for value in raw_stream_outputs
        ):
            raise v2._FormalQueryError(
                "RAGFLOW_TOOL_PROTOCOL_INVALID",
                502,
                "Workflow returned an invalid tool result",
            )
        accumulated = v2.sanitize_citation_markers(finalized.answer)
        # The Workflow endpoint never publishes raw model reasoning.
        reasoning = None
        status = _workflow_status_from(explicit_status, accumulated)
        if not accumulated:
            accumulated = v2.NO_RELIABLE_EVIDENCE_ANSWER
            status = "no_reliable_evidence"
        elif status == "failed":
            raise v2.RAGFlowAPIError(
                "RAGFlow Workflow reported a failed run", 502, run["run_id"]
            )
        elif status == "no_reliable_evidence" and explicit_status not in _VALID_STATUSES:
            # Older/third-party Agent API versions may omit a terminal status
            # frame. A non-empty final Message is still an explicit successful
            # workflow result; an empty stream remains no-reliable-evidence.
            status = "completed"
        if status != "completed":
            accumulated = v2.NO_RELIABLE_EVIDENCE_ANSWER
            reference = {"chunks": [], "doc_aggs": []}
        chunks = _workflow_chunks(reference)
        citations = v2._external_citations(
            chunks,
            docs_by_internal_id,
            assistant_message_id,
            answer=accumulated,
            status=status,
            internet_enabled=req.internetEnabled,
            attachment_document_ids=_workflow_attachment_ids(pending),
        )
        if status == "completed" and not _workflow_evidence_present(
            chunks, citations, pending
        ):
            status = "no_reliable_evidence"
            accumulated = v2.NO_RELIABLE_EVIDENCE_ANSWER
            reference = {"chunks": [], "doc_aggs": []}
            citations = []
        public_citations = await v2._project_citations(db, citations, request, principal)
        if accumulated != emitted_answer:
            # Canvas can send a decorated final Message after answer deltas
            # (for example when think tags were split differently). Replace the
            # client buffer once so the persisted/public answer is authoritative.
            yield v2._sse(
                "answer.replaced" if emitted_answer else "answer.delta",
                {
                    "conversationId": conversation["conversation_id"],
                    "runId": run["run_id"],
                    "content": accumulated,
                },
            )
        await v2._gw_write(
            db, v2.v2_store.add_message,
            message_id=assistant_message_id,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            role="assistant", content=accumulated, status=status,
            citations=citations, reasoning=reasoning or None,
        )
        if workflow_session_id:
            await v2._gw_write(
                db, v2.v2_store.set_workflow_session,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                agent_id=agent_id, version=version, session_id=workflow_session_id,
            )
        result = {
            "conversationId": conversation["conversation_id"],
            "clientMessageId": _public_request_id(req),
            "runId": run["run_id"], "messageId": assistant_message_id,
            "answer": accumulated, "reasoning": reasoning or None,
            "status": status, "citations": public_citations,
            "workflowVersion": version, "replayed": False,
        }
        diagnostics = _workflow_finish_trace(run, request, outcome=status)
        if diagnostics:
            result["_diagnostics"] = diagnostics
        await v2._gw_write(
            db, v2.v2_store.complete_message_run,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            client_message_id=internal_req.clientMessageId,
            result=result, status="completed", assistant_message_id=assistant_message_id,
        )
        if status == "completed":
            schedule_memory_candidate(
                principal, chat_id=f"workflow:{agent_id}",
                session_id=workflow_session_id or run["run_id"],
                user_input=question, agent_response=accumulated,
                request_id=run["run_id"],
            )
        for citation in public_citations:
            yield v2._sse("citation", citation)
        yield v2._sse(
            "answer.completed",
            {
                "conversationId": conversation["conversation_id"],
                "runId": run["run_id"], "messageId": assistant_message_id,
                "status": v2.v2_store.public_status(status),
                "citations": public_citations,
                "reasoning": reasoning,
            },
        )
    except asyncio.CancelledError:
        await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code="RUN_INTERRUPTED", status_code=503,
            message="Message run was interrupted before completion", request=request,
        )
        raise
    except (v2.RAGFlowAPIError, v2._FormalQueryError) as exc:
        if isinstance(exc, v2._FormalQueryError):
            code, status_code, message = exc.code, exc.status_code, exc.message
        else:
            mapped = v2._mapped_llm_provider_failure(str(exc))
            if mapped is not None:
                code, status_code, message = mapped
            else:
                code = (
                    "RAGFLOW_API_INCOMPATIBLE"
                    if exc.status_code
                    and (400 <= exc.status_code < 500 or exc.status_code == 502)
                    else "RAGFLOW_UNAVAILABLE"
                )
                status_code = 503
                message = "Query engine unavailable"
        await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code=code, status_code=status_code, message=message, request=request,
        )
        if emitted_answer:
            yield v2._sse(
                "answer.replaced",
                {
                    "conversationId": conversation["conversation_id"],
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield v2._sse(
            "run.failed",
            {
                "conversationId": conversation["conversation_id"],
                "runId": run["run_id"],
                "code": code,
                "message": message,
            },
        )
    except (v2.httpx.HTTPError, asyncio.TimeoutError, ConnectionError, OSError):
        await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code="RAGFLOW_UNAVAILABLE", status_code=503,
            message="Query engine unavailable", request=request,
        )
        if emitted_answer:
            yield v2._sse(
                "answer.replaced",
                {
                    "conversationId": conversation["conversation_id"],
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield v2._sse(
            "run.failed",
            {
                "conversationId": conversation["conversation_id"],
                "runId": run["run_id"],
                "code": "RAGFLOW_UNAVAILABLE",
                "message": "Query engine unavailable",
            },
        )
    except Exception:
        logger.exception("workflow stream failed")
        await v2._save_failed_run(
            db, principal, conversation, internal_req, run, assistant_message_id,
            code="INTERNAL_ERROR", status_code=500,
            message="Workflow message run failed", request=request,
        )
        if emitted_answer:
            yield v2._sse(
                "answer.replaced",
                {
                    "conversationId": conversation["conversation_id"],
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield v2._sse(
            "run.failed",
            {
                "conversationId": conversation["conversation_id"],
                "runId": run["run_id"],
                "code": "INTERNAL_ERROR",
                "message": "Workflow message run failed",
            },
        )
    finally:
        await cleanup_ragflow_files(pending, client, db)


@router.get("/status")
async def workflow_status(
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    del principal
    configured = _workflow_configuration()
    return {
        "enabled": configured is not None,
        "version": configured[1] if configured else None,
        "agentConfigured": bool(configured and configured[0]),
        "runtime": "ragflow-agent-workflow",
    }


@router.post("/conversations/{conversation_id}/messages")
async def create_workflow_message(
    conversation_id: str,
    request: Request,
    db=Depends(v2.get_db),
    principal: UserPrincipal = Depends(require_capability("ask", "view_citations")),
):
    try:
        req, pending = await _parse_request(request, principal)
    except v2._ReasoningModeDenied:
        return v2._error(
            403,
            "REASONING_MODE_NOT_ALLOWED",
            "This authentication source cannot use the selected reasoning mode",
        )
    except v2.TransientAttachmentError as exc:
        return v2._error(exc.status_code, exc.code, exc.message)
    except (ValidationError, ValueError, json.JSONDecodeError):
        return v2._error(422, "VALIDATION_ERROR", "Invalid workflow message")
    request.state.inquiry_audit_body = v2._inquiry_audit_body(req, pending)

    if _workflow_configuration() is None:
        return v2._error(
            503,
            "WORKFLOW_NOT_CONFIGURED",
            "Agent Workflow test runtime is not configured",
        )
    internal_req = _internal_request(req)
    lock = await v2._conversation_lock(conversation_id)
    async with lock:
        conversation = await v2._owned_conversation(db, principal, conversation_id)
        if not conversation:
            return v2._error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        if conversation["status"] == "archived":
            return v2._error(409, "CONVERSATION_ARCHIVED", "Conversation is archived")
        bound_session = str(conversation.get("workflow_session_id") or "").strip()
        configured = _workflow_configuration()
        if bound_session and configured and (
            str(conversation.get("workflow_agent_id") or "") != configured[0]
            or str(conversation.get("workflow_version") or "") != configured[1]
        ):
            return v2._error(
                409,
                "WORKFLOW_VERSION_MISMATCH",
                "Conversation is bound to another Workflow version",
            )
        conversation, question, run_or_result, error = await v2._prepare_message_run(
            db, principal, conversation, internal_req, pending
        )
        if error:
            return error
        if run_or_result is None:
            return v2._error(503, "RUN_INTERRUPTED", "Message run could not be prepared")
        if "answer" in run_or_result and "messageId" in run_or_result:
            result = dict(run_or_result)
            result["clientMessageId"] = req.clientMessageId
            return v2._public_run_payload(result, result.get("citations", []))
        run = run_or_result
        if pending:
            await v2._persist_pending_attachments(db, run, pending)
    if "text/event-stream" in request.headers.get("accept", "").lower():
        return StreamingResponse(
            _workflow_stream(
                db, principal, conversation, req, internal_req,
                question, run, pending, request,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result, error = await _workflow_run_result(
        db, principal, conversation, req, internal_req,
        question, run, pending, request,
    )
    if error:
        return error
    return v2._public_run_payload(result or {}, (result or {}).get("citations", []))
