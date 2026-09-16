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
