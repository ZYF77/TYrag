import time

from rag import diagnostics


def test_disabled_sink_is_empty_and_does_not_raise():
    token = diagnostics.begin_rag_diagnostics(False, "run-disabled")
    try:
        diagnostics.record_rag_diagnostics("request", {"query": "question"})
        assert diagnostics.snapshot_rag_diagnostics() == {}
    finally:
        diagnostics.reset_rag_diagnostics(token)


def test_enabled_sink_keeps_safe_metadata_and_drops_bodies():
    token = diagnostics.begin_rag_diagnostics(True, "run-1")
    try:
        diagnostics.record_rag_diagnostics(
            "retrieval",
            {
                "query": "设备为什么报警？",
                "chunkId": "chunk-1",
                "score": 0.91,
                "prompt": "must-not-leak",
                "content_with_weight": "must-not-leak",
                "response": "must-not-leak",
            },
        )
        snapshot = diagnostics.snapshot_rag_diagnostics()
    finally:
        diagnostics.reset_rag_diagnostics(token)

    assert snapshot["runId"] == "run-1"
    assert snapshot["events"][0]["data"] == {
        "query": "设备为什么报警？",
        "chunkId": "chunk-1",
        "score": 0.91,
    }
    assert snapshot["truncated"] is True
    assert "must-not-leak" not in str(snapshot)


def test_trace_caps_events_and_marks_truncated():
    token = diagnostics.begin_rag_diagnostics(True, "run-capped")
    try:
        for index in range(300):
            diagnostics.record_rag_diagnostics("llm", {"index": index})
        snapshot = diagnostics.snapshot_rag_diagnostics()
    finally:
        diagnostics.reset_rag_diagnostics(token)

    assert len(snapshot["events"]) == 256
    assert snapshot["truncated"] is True


def test_sink_failure_is_isolated_from_caller():
    class BrokenSink:
        def record(self, event_type, payload):
            raise RuntimeError("diagnostics failed")

        def snapshot(self):
            raise RuntimeError("diagnostics failed")

    token = diagnostics._CURRENT_SINK.set(BrokenSink())
    try:
        diagnostics.record_rag_diagnostics("request", {"query": "safe"})
        assert diagnostics.snapshot_rag_diagnostics() == {}
    finally:
        diagnostics._CURRENT_SINK.reset(token)


def test_timed_stage_exposes_step_duration_and_stage_for_llm_calls():
    token = diagnostics.begin_rag_diagnostics(True, "run-timing")
    try:
        with diagnostics.rag_diagnostics_stage("keyword_analysis"):
            diagnostics.record_rag_diagnostics(
                "llm",
                {"callType": "chat", "durationMs": 12.5, "status": "success"},
            )
        diagnostics.record_timed_rag_stage(
            "rerank", time.perf_counter() - 0.001, enabled=True, executed=True
        )
        snapshot = diagnostics.snapshot_rag_diagnostics()
    finally:
        diagnostics.reset_rag_diagnostics(token)

    assert snapshot["timing"] == {
        "atMs": "cumulative_from_trace_start",
        "durationMs": "current_event",
    }
    assert snapshot["events"][0]["data"]["stage"] == "keyword_analysis"
    assert snapshot["events"][0]["durationMs"] == 12.5
    assert snapshot["events"][1]["data"]["stage"] == "rerank"
    assert snapshot["events"][1]["durationMs"] >= 0
