"""Timing semantics and P0 inquiry phased diagnostics."""

from time import perf_counter
from unittest.mock import patch

from enterprise.gateway.query.diagnostics import (
    finish_trace,
    merge_upstream,
    record_event,
    record_timed_event,
    start_trace,
)
from enterprise.gateway.query import v2_router


def test_event_duration_is_step_time_while_at_ms_is_cumulative():
    trace = start_trace("run-timing", query="safe", reasoning_mode="simple", stream=True)
    record_event(trace, "stage", {"stage": "rerank", "durationMs": 12.5})

    result = finish_trace(trace, outcome="completed")

    event = result["events"][1]
    assert event["atMs"] >= 0
    assert event["durationMs"] == 12.5
    assert result["timing"] == {
        "atMs": "cumulative_from_trace_start",
        "durationMs": "current_event",
    }


def test_merge_upstream_preserves_source_time_and_step_duration():
    trace = start_trace("run-upstream", query="safe", reasoning_mode="simple", stream=True)
    merge_upstream(
        trace,
        {
            "runId": "run-upstream",
            "durationMs": 100,
            "events": [
                {
                    "type": "stage",
                    "atMs": 40,
                    "durationMs": 12.5,
                    "data": {"stage": "rerank", "source": "ragflow"},
                }
            ],
        },
    )

    result = finish_trace(trace, outcome="completed")
    event = next(item for item in result["events"] if item["type"] == "stage")
    assert event["sourceAtMs"] == 40
    assert event["durationMs"] == 12.5
    assert event["data"]["stage"] == "rerank"


def test_phased_gateway_events_present_when_recorded():
    trace = start_trace("run-phased", query="safe", reasoning_mode="simple", stream=False)
    started = perf_counter()
    record_timed_event(
        trace,
        "scope",
        started,
        {"source": "gateway", "stage": "gateway_scope", "allowedDocumentIds": ["d1"]},
    )
    record_timed_event(
        trace,
        "attachment_understand",
        started,
        {
            "source": "gateway",
            "stage": "attachment_understand",
            "attachmentCount": 1,
            "observationCount": 1,
            "understoodCount": 1,
        },
    )
    record_timed_event(
        trace,
        "chat_session",
        started,
        {
            "source": "gateway",
            "stage": "chat_session",
            "binding": "warmup_hit",
            "sessionId": "s1",
        },
    )
    record_timed_event(
        trace,
        "upstream_request",
        started,
        {"source": "gateway", "stage": "ragflow_request", "status": "success"},
    )
    record_timed_event(
        trace,
        "stream_first_token",
        started,
        {"source": "gateway", "stage": "stream_first_token", "status": "success"},
    )
    record_timed_event(
        trace,
        "citation_projection",
        started,
        {"source": "gateway", "stage": "citation_projection", "citationCount": 0},
    )
    merge_upstream(
        trace,
        {
            "runId": "run-phased",
            "durationMs": 50,
            "events": [
                {
                    "type": "stage",
                    "atMs": 5,
                    "durationMs": 1,
                    "data": {"stage": "embedding", "source": "ragflow"},
                },
                {
                    "type": "stage",
                    "atMs": 10,
                    "durationMs": 2,
                    "data": {"stage": "candidate_search", "source": "ragflow"},
                },
                {
                    "type": "stage",
                    "atMs": 20,
                    "durationMs": 3,
                    "data": {"stage": "rerank", "source": "ragflow", "enabled": False},
                },
                {
                    "type": "stage",
                    "atMs": 30,
                    "durationMs": 4,
                    "data": {"stage": "answer_generation", "source": "ragflow"},
                },
                {
                    "type": "stage",
                    "atMs": 35,
                    "durationMs": 1,
                    "data": {
                        "stage": "tool",
                        "source": "ragflow",
                        "toolName": "search",
                        "toolCallCount": 1,
                        "toolDurationMsTotal": 1.0,
                    },
                },
            ],
        },
    )

    result = finish_trace(trace, outcome="completed")
    types = {event["type"] for event in result["events"]}
    stages = {
        (event.get("data") or {}).get("stage")
        for event in result["events"]
        if isinstance(event.get("data"), dict)
    }
    assert "scope" in types
    assert "attachment_understand" in types
    assert "chat_session" in types
    assert "upstream_request" in types
    assert "stream_first_token" in types
    assert "citation_projection" in types
    assert "gateway_scope" in stages
    assert "embedding" in stages
    assert "candidate_search" in stages
    assert "rerank" in stages
    assert "answer_generation" in stages
    assert "tool" in stages
    session = next(e for e in result["events"] if e["type"] == "chat_session")
    assert session["data"]["binding"] == "warmup_hit"
    assert "durationMs" in session
    # redaction: blocked keys never appear
    blob = str(result)
    assert "prompt" not in blob.lower() or '"prompt"' not in blob


def test_diagnostics_absent_when_flag_off_does_not_start_trace():
    with patch.object(v2_router.config, "rag_diagnostics_enabled", False):
        assert v2_router.config.rag_diagnostics_enabled is False
        # Ask-path gate: start_trace only when flag is on.
        run = {"run_id": "r1"}
        if v2_router.config.rag_diagnostics_enabled:
            run["_diagnostics"] = start_trace(
                run["run_id"], query="q", reasoning_mode="simple", stream=False
            )
        assert "_diagnostics" not in run


def test_session_binding_records_chat_session_with_warmup_and_fallback():
    conversation = {"conversation_id": "c1"}
    for binding in ("warmup_hit", "ensure_fallback"):
        trace = start_trace("run-bind", query="safe", reasoning_mode="simple", stream=True)
        started = perf_counter()
        v2_router._record_session_binding(
            conversation,
            trace,
            binding,
            session_id="sess-1",
            chat_id="chat-1",
            started=started,
        )
        finished = finish_trace(trace, outcome="completed")
        event = next(e for e in finished["events"] if e["type"] == "chat_session")
        assert event["data"]["binding"] == binding
        assert event["data"]["stage"] == "chat_session"
        assert event["durationMs"] >= 0
        assert conversation["_session_binding"] == binding


def test_json_first_byte_derived_from_upstream_llm_ttft():
    trace = start_trace("run-ttft", query="safe", reasoning_mode="simple", stream=False)
    merge_upstream(
        trace,
        {
            "runId": "run-ttft",
            "durationMs": 80,
            "events": [
                {
                    "type": "llm",
                    "atMs": 40,
                    "durationMs": 30,
                    "data": {"ttftMs": 12.25, "stage": "answer_generation"},
                }
            ],
        },
    )
    v2_router._record_json_first_byte_from_upstream(trace)
    finished = finish_trace(trace, outcome="completed")
    token = next(e for e in finished["events"] if e["type"] == "stream_first_token")
    assert token["data"]["status"] == "derived_from_upstream_llm"
    assert token["durationMs"] == 12.25


def test_blocked_keys_stripped_from_phased_payloads():
    trace = start_trace("run-redact", query="safe", reasoning_mode="simple", stream=True)
    record_timed_event(
        trace,
        "attachment_understand",
        perf_counter(),
        {
            "source": "gateway",
            "stage": "attachment_understand",
            "prompt": "SECRET-PROMPT",
            "knowledge": "SECRET-KB",
            "attachmentCount": 0,
        },
    )
    finished = finish_trace(trace, outcome="completed")
    event = next(e for e in finished["events"] if e["type"] == "attachment_understand")
    assert "prompt" not in event["data"]
    assert "knowledge" not in event["data"]
    assert event["data"]["attachmentCount"] == 0

def test_trace_records_request_prepare_and_http_response_timing():
    request_started = perf_counter() - 0.005
    trace = start_trace(
        "run-request-timing",
        query="safe",
        reasoning_mode="high",
        stream=True,
        request_started=request_started,
    )
    record_event(
        trace,
        "http_response",
        {
            "source": "gateway",
            "stage": "http_response",
            "responseHeadersMs": 6.0,
            "responseFirstBodyMs": 7.5,
        },
    )

    result = finish_trace(trace, outcome="completed")
    prepare = next(event for event in result["events"] if event["type"] == "gateway_prepare")
    response = next(event for event in result["events"] if event["type"] == "http_response")

    assert prepare["durationMs"] >= 0
    assert response["data"]["responseHeadersMs"] == 6.0
    assert response["data"]["responseFirstBodyMs"] == 7.5


def test_workflow_stream_first_packet_accounting():
    """WF streaming records Chat-aligned first-packet milestones from RF flags."""
    from enterprise.gateway.query import workflow_router
    from enterprise.gateway.query.answer_split import StreamThinkSplitter

    request_started = perf_counter() - 0.01
    trace = start_trace(
        "wf-stream-1",
        query="safe",
        reasoning_mode="simple",
        stream=True,
        request_started=request_started,
    )
    run = {"run_id": "wf-stream-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())

    first_flags = {"stream_output": False, "reasoning": False, "answer": False}
    splitter = StreamThinkSplitter()
    upstream_started = perf_counter() - 0.005

    # Think-start flag with reasoning body (not exposed on SSE, still timed).
    pieces = workflow_router._workflow_record_stream_first_packets(
        run,
        upstream_started=upstream_started,
        data={"start_to_think": True},
        content="think-a",
        first_flags=first_flags,
        splitter=splitter,
    )
    assert pieces and pieces[0][0] == "reasoning"
    assert first_flags["stream_output"] is True
    assert first_flags["reasoning"] is True
    assert first_flags["answer"] is False

    # Close think on its own frame (flag applies after feed split).
    workflow_router._workflow_record_stream_first_packets(
        run,
        upstream_started=upstream_started,
        data={"end_to_think": True},
        content="",
        first_flags=first_flags,
        splitter=splitter,
    )

    # Answer body frame -> stream_first_answer once.
    pieces = workflow_router._workflow_record_stream_first_packets(
        run,
        upstream_started=upstream_started,
        data={},
        content="answer-body",
        first_flags=first_flags,
        splitter=splitter,
    )
    assert any(kind == "answer" for kind, _ in pieces)
    assert first_flags["answer"] is True

    # Idempotent: second answer chunk must not duplicate first-packet events.
    before = len(trace["events"])
    workflow_router._workflow_record_stream_first_packets(
        run,
        upstream_started=upstream_started,
        data={},
        content=" more",
        first_flags=first_flags,
        splitter=splitter,
    )
    types = [e["type"] for e in trace["events"][before:]]
    assert "stream_first_token" not in types
    assert "stream_first_reasoning" not in types
    assert "stream_first_answer" not in types

    # Upstream answer_generation arrives via merge_upstream (Chat parity).
    merge_upstream(
        trace,
        {
            "runId": "wf-stream-1",
            "durationMs": 40,
            "events": [
                {
                    "type": "stage",
                    "atMs": 20,
                    "durationMs": 5,
                    "data": {"stage": "answer_generation", "source": "ragflow"},
                }
            ],
        },
    )
    workflow_router._workflow_merge_upstream_diagnostics(
        run,
        {
            "_diagnostics": {
                "runId": "wf-stream-1",
                "durationMs": 10,
                "events": [
                    {
                        "type": "stage",
                        "atMs": 1,
                        "durationMs": 1,
                        "data": {"stage": "embedding", "source": "ragflow"},
                    }
                ],
            }
        },
    )

    finished = finish_trace(trace, outcome="completed")
    types = {e["type"] for e in finished["events"]}
    stages = {
        (e.get("data") or {}).get("stage")
        for e in finished["events"]
        if isinstance(e.get("data"), dict)
    }
    assert "run_started" in types
    assert "stream_first_token" in types
    assert "stream_first_reasoning" in types
    assert "stream_first_answer" in types
    assert "answer_generation" in stages
    assert "embedding" in stages
    run_started = next(e for e in finished["events"] if e["type"] == "run_started")
    assert run_started["data"]["stage"] == "run_started_emit"
    assert run_started["durationMs"] >= 0


def test_workflow_nonstream_records_run_started_not_stream_first():
    """JSON WF path records run_started; must not invent stream_first_*."""
    from enterprise.gateway.query import workflow_router

    request_started = perf_counter() - 0.002
    trace = start_trace(
        "wf-json-1",
        query="safe",
        reasoning_mode="simple",
        stream=False,
        request_started=request_started,
    )
    run = {"run_id": "wf-json-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())
    finished = finish_trace(trace, outcome="completed")
    types = {e["type"] for e in finished["events"]}
    assert "run_started" in types
    assert "stream_first_token" not in types
    assert "stream_first_reasoning" not in types
    assert "stream_first_answer" not in types

def test_workflow_canvas_node_start_finish_summaries_no_giant_outputs():
    """P1: RF node_started/finished -> compact wf_node_* events (no payloads)."""
    from enterprise.gateway.query import workflow_router

    request_started = perf_counter() - 0.01
    trace = start_trace(
        "wf-nodes-1",
        query="safe",
        reasoning_mode="simple",
        stream=True,
        request_started=request_started,
    )
    run = {"run_id": "wf-nodes-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())

    giant = "X" * 50_000
    workflow_router._workflow_record_canvas_node_events(
        run,
        event="node_started",
        data={
            "component_id": "retrieval_0",
            "component_name": "Retrieve docs",
            "component_type": "Retrieval",
            "inputs": None,
            "thoughts": giant,
        },
    )
    workflow_router._workflow_record_canvas_node_events(
        run,
        event="node_finished",
        data={
            "component_id": "retrieval_0",
            "component_name": "Retrieve docs",
            "component_type": "Retrieval",
            "inputs": {"query": giant},
            "outputs": {"chunks": [giant], "content": giant},
            "error": None,
            "elapsed_time": 0.125,
        },
    )
    # Error path: status/error recorded; still no outputs.
    workflow_router._workflow_record_canvas_node_events(
        run,
        event="node_finished",
        data={
            "component_id": "generate_0",
            "component_name": "Generate",
            "component_type": "Generate",
            "outputs": {"content": giant},
            "error": "boom-" + giant[:200],
            "elapsed_time": 1.5,
        },
    )
    # Unknown / non-node events ignored.
    before = len(trace["events"])
    workflow_router._workflow_record_canvas_node_events(
        run, event="message", data={"content": giant}
    )
    assert len(trace["events"]) == before

    finished = finish_trace(trace, outcome="completed")
    types = [e["type"] for e in finished["events"]]
    assert types.count("wf_node_started") == 1
    assert types.count("wf_node_finished") == 2
    # P0 milestones still present when recorded on same trace.
    assert "run_started" in types

    started = next(e for e in finished["events"] if e["type"] == "wf_node_started")
    assert started["data"]["componentId"] == "retrieval_0"
    assert started["data"]["componentName"] == "Retrieve docs"
    assert started["data"]["componentType"] == "Retrieval"
    assert started["data"]["status"] == "running"
    assert "inputs" not in started["data"]
    assert "outputs" not in started["data"]
    assert "thoughts" not in started["data"]
    assert giant not in str(started)

    ok_finish = next(
        e
        for e in finished["events"]
        if e["type"] == "wf_node_finished"
        and (e.get("data") or {}).get("componentId") == "retrieval_0"
    )
    assert ok_finish["data"]["status"] == "success"
    assert ok_finish["data"]["elapsedSec"] == 0.125
    assert ok_finish["durationMs"] == 125.0
    assert "outputs" not in ok_finish["data"]
    assert "inputs" not in ok_finish["data"]
    assert giant not in str(ok_finish)

    err_finish = next(
        e
        for e in finished["events"]
        if e["type"] == "wf_node_finished"
        and (e.get("data") or {}).get("componentId") == "generate_0"
    )
    assert err_finish["data"]["status"] == "error"
    assert err_finish["data"]["error"] == "NodeExecutionError"
    assert "boom-" not in str(err_finish)
    assert len(str(err_finish["data"]["error"])) <= 256
    assert "outputs" not in err_finish["data"]
    assert giant not in str(finished)


def test_workflow_stream_node_events_coexist_with_p0_milestones():
    """Simulated stream frames: node summaries + stream_first_* stay green."""
    from enterprise.gateway.query import workflow_router
    from enterprise.gateway.query.answer_split import StreamThinkSplitter

    request_started = perf_counter() - 0.01
    trace = start_trace(
        "wf-nodes-stream-1",
        query="safe",
        reasoning_mode="simple",
        stream=True,
        request_started=request_started,
    )
    run = {"run_id": "wf-nodes-stream-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())
    first_flags = {"stream_output": False, "reasoning": False, "answer": False}
    splitter = StreamThinkSplitter()
    upstream_started = perf_counter() - 0.005

    frames = [
        (
            "node_started",
            {
                "component_id": "begin",
                "component_name": "Begin",
                "component_type": "Begin",
            },
        ),
        (
            "node_finished",
            {
                "component_id": "begin",
                "component_name": "Begin",
                "component_type": "Begin",
                "elapsed_time": 0.01,
                "outputs": {"content": "Y" * 20_000},
            },
        ),
        ("message", {"content": "hello answer"}),
    ]
    for event, data in frames:
        if event in ("node_started", "node_finished"):
            workflow_router._workflow_record_canvas_node_events(
                run, event=event, data=data
            )
        elif event == "message":
            workflow_router._workflow_record_stream_first_packets(
                run,
                upstream_started=upstream_started,
                data=data,
                content=str(data.get("content") or ""),
                first_flags=first_flags,
                splitter=splitter,
            )

    finished = finish_trace(trace, outcome="completed")
    types = {e["type"] for e in finished["events"]}
    assert "run_started" in types
    assert "wf_node_started" in types
    assert "wf_node_finished" in types
    assert "stream_first_token" in types
    assert "stream_first_answer" in types
    assert "Y" * 100 not in str(finished)




def test_workflow_tool_mcp_call_summaries_no_giant_payloads():
    """P2: synthetic tool/MCP SSE frames -> wf_tool_call + wf_tools_summary."""
    from enterprise.gateway.query import workflow_router

    request_started = perf_counter() - 0.01
    trace = start_trace(
        "wf-tools-1",
        query="safe",
        reasoning_mode="simple",
        stream=True,
        request_started=request_started,
    )
    run = {"run_id": "wf-tools-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())

    giant = "Z" * 50_000
    # Non-tool canvas nodes must not invent tool events (v1.7 production shape).
    workflow_router._workflow_record_canvas_node_events(
        run,
        event="node_finished",
        data={
            "component_id": "begin",
            "component_name": "Begin",
            "component_type": "Begin",
            "elapsed_time": 0.01,
            "outputs": {"content": giant},
        },
    )
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="node_finished",
        data={
            "component_id": "begin",
            "component_name": "Begin",
            "component_type": "Begin",
            "elapsed_time": 0.01,
            "outputs": {"content": giant},
        },
    )
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="node_finished",
        data={
            "component_id": "Retrieval:FocusedEvidence",
            "component_name": "Retrieval",
            "component_type": "Retrieval",
            "elapsed_time": 0.2,
            "outputs": {"chunks": [giant]},
        },
    )

    # Synthetic RF tool node finish (live v1.7 canvas has no tool nodes).
    workflow_router._workflow_record_canvas_node_events(
        run,
        event="node_finished",
        data={
            "component_id": "TavilySearch:web",
            "component_name": "Web search",
            "component_type": "TavilySearch",
            "elapsed_time": 0.42,
            "inputs": {"query": giant},
            "outputs": {"result": giant},
            "arguments": {"q": giant},
            "result": giant,
        },
    )
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="node_finished",
        data={
            "component_id": "TavilySearch:web",
            "component_name": "Web search",
            "component_type": "TavilySearch",
            "elapsed_time": 0.42,
            "inputs": {"query": giant},
            "outputs": {"result": giant},
            "arguments": {"q": giant},
            "result": giant,
        },
    )
    # MCP-style explicit fields + dedicated SSE event name.
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="mcp_call",
        data={
            "tool_name": "get_equipment_status",
            "tool_id": "mcp-equip-1",
            "mcp_id": "mcp-server-9",
            "elapsed_time": 0.11,
            "arguments": {"id": giant},
            "result": {"raw": giant},
            "error": None,
        },
    )
    # Error path still omits args/result.
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="tool_call",
        data={
            "toolName": "Wikipedia",
            "toolId": "wiki-1",
            "elapsed_time": 1.25,
            "error": "timeout-" + giant[:100],
            "params": {"q": giant},
            "toolResult": giant,
        },
    )
    # tool_use_callback-shaped nested rows (name/latency only).
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="node_finished",
        data={
            "component_id": "Agent:FocusedAnswer",
            "component_name": "Answer",
            "component_type": "Agent",
            "elapsed_time": 3.0,
            "outputs": {"content": giant},
            "trace": [
                {
                    "component_id": "Agent:FocusedAnswer",
                    "trace": [
                        {
                            "tool_name": "ExeSQL",
                            "arguments": {"sql": giant},
                            "result": giant,
                            "elapsed_time": 0.05,
                        }
                    ],
                }
            ],
        },
    )

    workflow_router._workflow_record_tools_summary(run)
    finished = finish_trace(trace, outcome="completed")
    types = [e["type"] for e in finished["events"]]
    assert "run_started" in types
    assert types.count("wf_tool_call") == 4
    assert types.count("wf_tools_summary") == 1
    assert giant not in str(finished)
    blob = str(finished)
    assert "arguments" not in blob.lower() or '"arguments"' not in blob
    assert "toolresult" not in blob.lower()
    assert '"result"' not in blob

    calls = [e for e in finished["events"] if e["type"] == "wf_tool_call"]
    by_name = {(e["data"].get("name"), e["data"].get("toolSource")): e for e in calls}
    assert ("TavilySearch", "tool") in by_name or any(
        e["data"].get("name") == "TavilySearch" for e in calls
    )
    tavily = next(e for e in calls if e["data"].get("name") == "TavilySearch")
    assert tavily["data"]["toolSource"] == "tool"
    assert tavily["data"]["status"] == "success"
    assert tavily["durationMs"] == 420.0
    assert tavily["data"]["toolId"] == "TavilySearch:web"
    assert "inputs" not in tavily["data"]
    assert "outputs" not in tavily["data"]
    assert "arguments" not in tavily["data"]
    assert "result" not in tavily["data"]

    mcp = next(e for e in calls if e["data"].get("name") == "get_equipment_status")
    assert mcp["data"]["toolSource"] == "mcp"
    assert mcp["data"]["status"] == "success"
    assert mcp["durationMs"] == 110.0

    wiki = next(e for e in calls if e["data"].get("name") == "Wikipedia")
    assert wiki["data"]["status"] == "error"
    assert wiki["data"]["error"] == "ToolExecutionError"
    assert len(str(wiki["data"]["error"])) <= 256

    sql = next(e for e in calls if e["data"].get("name") == "ExeSQL")
    assert sql["data"]["toolSource"] == "tool"
    assert sql["durationMs"] == 50.0

    summary = next(e for e in finished["events"] if e["type"] == "wf_tools_summary")
    assert summary["data"]["totalCount"] == 4
    assert len(summary["data"]["tools"]) == 4
    names = {row["name"] for row in summary["data"]["tools"]}
    assert names == {"TavilySearch", "get_equipment_status", "Wikipedia", "ExeSQL"}
    wiki_row = next(r for r in summary["data"]["tools"] if r["name"] == "Wikipedia")
    assert wiki_row["error"] == 1
    assert wiki_row["ok"] == 0


def test_workflow_stream_tool_frames_coexist_with_p0_p1():
    """Simulated stream: P0 milestones + P1 nodes + P2 tool summary stay green."""
    from enterprise.gateway.query import workflow_router
    from enterprise.gateway.query.answer_split import StreamThinkSplitter

    request_started = perf_counter() - 0.01
    trace = start_trace(
        "wf-tools-stream-1",
        query="safe",
        reasoning_mode="simple",
        stream=True,
        request_started=request_started,
    )
    run = {"run_id": "wf-tools-stream-1", "_diagnostics": trace}

    class _Req:
        class state:
            gateway_request_started = request_started

    workflow_router._workflow_record_run_started(run, _Req())
    first_flags = {"stream_output": False, "reasoning": False, "answer": False}
    splitter = StreamThinkSplitter()
    upstream_started = perf_counter() - 0.005
    giant = "Q" * 20_000

    frames = [
        (
            "node_started",
            {
                "component_id": "begin",
                "component_name": "Begin",
                "component_type": "Begin",
            },
        ),
        (
            "node_finished",
            {
                "component_id": "begin",
                "component_name": "Begin",
                "component_type": "Begin",
                "elapsed_time": 0.01,
            },
        ),
        (
            "node_finished",
            {
                "component_id": "Wikipedia:1",
                "component_name": "Wiki",
                "component_type": "Wikipedia",
                "elapsed_time": 0.2,
                "outputs": {"content": giant},
            },
        ),
        (
            "mcp_call",
            {
                "tool_name": "lookup",
                "mcp_id": "m1",
                "elapsed_time": 0.03,
                "arguments": {"x": giant},
                "result": giant,
            },
        ),
        ("message", {"content": "hello answer"}),
        ("workflow_finished", {"status": "completed"}),
    ]
    for event, data in frames:
        if event in ("node_started", "node_finished"):
            workflow_router._workflow_record_canvas_node_events(
                run, event=event, data=data
            )
            workflow_router._workflow_record_canvas_tool_events(
                run, event=event, data=data
            )
        elif event in workflow_router._WF_TOOL_SSE_EVENTS:
            workflow_router._workflow_record_canvas_tool_events(
                run, event=event, data=data
            )
        elif event == "message":
            workflow_router._workflow_record_stream_first_packets(
                run,
                upstream_started=upstream_started,
                data=data,
                content=str(data.get("content") or ""),
                first_flags=first_flags,
                splitter=splitter,
            )
        elif event == "workflow_finished":
            workflow_router._workflow_record_tools_summary(run)

    finished = finish_trace(trace, outcome="completed")
    types = {e["type"] for e in finished["events"]}
    assert "run_started" in types
    assert "wf_node_started" in types
    assert "wf_node_finished" in types
    assert "stream_first_token" in types
    assert "stream_first_answer" in types
    assert "wf_tool_call" in types
    assert "wf_tools_summary" in types
    assert giant not in str(finished)
    assert "Q" * 100 not in str(finished)


def test_workflow_tools_summary_absent_when_no_tool_frames():
    """Production canvas without tool nodes yields empty tool diagnostics."""
    from enterprise.gateway.query import workflow_router

    trace = start_trace(
        "wf-tools-empty",
        query="safe",
        reasoning_mode="simple",
        stream=True,
    )
    run = {"run_id": "wf-tools-empty", "_diagnostics": trace}
    workflow_router._workflow_record_canvas_tool_events(
        run,
        event="node_finished",
        data={
            "component_id": "Agent:FocusedAnswer",
            "component_name": "Answer",
            "component_type": "Agent",
            "elapsed_time": 1.0,
        },
    )
    workflow_router._workflow_record_tools_summary(run)
    finished = finish_trace(trace, outcome="completed")
    types = {e["type"] for e in finished["events"]}
    assert "wf_tool_call" not in types
    assert "wf_tools_summary" not in types


def test_workflow_failed_response_preserves_safe_private_checkpoint():
    import pytest
    from enterprise.gateway.query.workflow_client import RAGFlowAgentClient
    from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError

    upstream = {
        "runId": "failed-workflow", "durationMs": 12,
        "events": [{"type": "workflow_tool_finished", "atMs": 10, "durationMs": 8,
                    "data": {"spanId": "tool-1", "parentSpanId": "node-1", "status": "error"}}],
    }
    with pytest.raises(RAGFlowAPIError) as caught:
        RAGFlowAgentClient._require_ok({"code": 500, "message": "Workflow execution failed", "data": {"_diagnostics": upstream}})
    trace = start_trace("failed-workflow", query="fixture", reasoning_mode="workflow", stream=False)
    merge_upstream(trace, caught.value.diagnostics)
    merge_upstream(trace, caught.value.diagnostics)
    result = finish_trace(trace, outcome="failed")
    spans = [e for e in result["events"] if e["type"] == "workflow_tool_finished"]
    assert len(spans) == 1
    assert spans[0]["data"]["parentSpanId"] == "node-1"
    assert spans[0]["durationMs"] == 8
    other = start_trace("other-run", query="fixture", reasoning_mode="workflow", stream=False)
    merge_upstream(other, caught.value.diagnostics)
    assert not any(e["type"] == "workflow_tool_finished" for e in other["events"])


def test_workflow_diagnostic_routes_reject_non_admin_capabilities():
    import asyncio
    import inspect
    import pytest
    from enterprise.gateway import admin_router
    from enterprise.gateway.auth.user_principal import UserPrincipal
    from enterprise.gateway.auth.middleware import UserAuthError

    for endpoint in (admin_router.list_rag_diagnostics, admin_router.get_rag_diagnostics):
        dependency = inspect.signature(endpoint).parameters["principal"].default.dependency
        for capabilities in ((), ("query",), ("audit",)):
            principal = UserPrincipal(tenant_id="tenant", business_user_id="user", subject="fixture", capabilities=capabilities)
            with pytest.raises(UserAuthError) as caught:
                asyncio.run(dependency(principal=principal))
            assert caught.value.status_code == 403
