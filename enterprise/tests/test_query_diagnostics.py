"""Timing semantics for private query diagnostics."""

from enterprise.gateway.query.diagnostics import (
    finish_trace,
    merge_upstream,
    record_event,
    start_trace,
)


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
