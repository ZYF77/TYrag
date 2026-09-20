"""Service-free tests for the Gateway Workflow terminal event contract."""

from __future__ import annotations

import pytest

from enterprise.gateway.query.workflow_events import (
    WorkflowEventCollector,
    WorkflowEventError,
    WorkflowInterrupted,
    WorkflowProtocolError,
)


def _event(name: str, **data: object) -> dict:
    return {"event": name, "session_id": "session-test", "data": data}


def _collect(events: list[dict]) -> dict:
    collector = WorkflowEventCollector()
    for payload in events:
        collector.consume(payload)
    return collector.finalize()


def test_usage_only_workflow_finished_keeps_message_status_and_body() -> None:
    result = _collect(
        [
            _event("workflow_started"),
            _event("message", content="部分回答"),
            _event("message_end", status="completed"),
            _event("workflow_finished", usage={"total_tokens": 7}),
        ]
    )

    assert result["event"] == "workflow_finished"
    assert result["data"]["status"] == "completed"
    assert result["data"]["content"] == "部分回答"
    assert result["data"]["usage"] == {"total_tokens": 7}
    assert result["data"]["_workflow_complete"] is True


def test_compatibility_terminal_status_may_be_on_event_envelope() -> None:
    result = _collect(
        [
            {"event": "message", "data": {"content": "正文"}},
            {"event": "workflow_finished", "status": "completed", "data": {}},
        ]
    )

    assert result["data"]["status"] == "completed"


def test_json_and_sse_event_sequences_have_the_same_terminal_result() -> None:
    events = [
        _event("workflow_started"),
        _event("message", content="第一段"),
        _event("message", content="第二段"),
        _event("message_end", status="no_reliable_evidence"),
        _event("workflow_finished", usage={"total_tokens": 9}),
    ]

    json_result = _collect(events)
    sse_collector = WorkflowEventCollector()
    for payload in events:
        sse_collector.consume(payload)
    sse_result = sse_collector.finalize()

    assert sse_result == json_result
    assert json_result["data"]["status"] == "no_reliable_evidence"
    assert json_result["data"]["content"] == "第一段第二段"


def test_missing_workflow_finished_is_interrupted_even_with_non_empty_body() -> None:
    collector = WorkflowEventCollector()
    collector.consume(_event("message", content="看起来像答案"))
    collector.consume(_event("message_end", status="completed"))
    assert collector.partial()["content"] == "看起来像答案"

    with pytest.raises(WorkflowInterrupted) as caught:
        collector.finalize()

    assert caught.value.code == "RUN_INTERRUPTED"
    assert caught.value.status_code == 503


def test_done_marker_without_events_is_not_a_terminal_result() -> None:
    # The transport drops the literal [DONE] marker before it reaches the
    # collector; an otherwise empty stream therefore remains interrupted.
    with pytest.raises(WorkflowInterrupted):
        WorkflowEventCollector().finalize()


@pytest.mark.parametrize("event_name", ["workflow_cancelled", "canceled"])
def test_explicit_cancellation_marker_is_interrupted(event_name: str) -> None:
    with pytest.raises(WorkflowInterrupted) as caught:
        WorkflowEventCollector().consume(_event(event_name))
    assert caught.value.code == "RUN_INTERRUPTED"


def test_cancelled_terminal_status_is_interrupted() -> None:
    with pytest.raises(WorkflowInterrupted) as caught:
        WorkflowEventCollector().consume(_event("workflow_finished", status="cancelled"))
    assert caught.value.code == "RUN_INTERRUPTED"

    with pytest.raises(WorkflowInterrupted) as caught:
        WorkflowEventCollector().consume(
            _event("workflow_finished", outputs="Task has been canceled")
        )
    assert caught.value.code == "RUN_INTERRUPTED"


def test_cancelled_message_end_status_is_interrupted() -> None:
    with pytest.raises(WorkflowInterrupted) as caught:
        WorkflowEventCollector().consume(_event("message_end", status="cancelled"))
    assert caught.value.code == "RUN_INTERRUPTED"


def test_failed_status_is_monotonic_after_later_completed_frame() -> None:
    result = _collect(
        [
            _event("message", content="失败前的正文"),
            _event("message_end", status="failed"),
            _event("workflow_finished", status="completed"),
        ]
    )

    assert result["data"]["status"] == "failed"
    assert result["data"]["content"] == "失败前的正文"


def test_last_complete_message_status_is_used_when_no_terminal_status_exists() -> None:
    result = _collect(
        [
            _event("message_end", status="completed"),
            _event("message_end", status="no_reliable_evidence"),
            _event("workflow_finished"),
        ]
    )

    assert result["data"]["status"] == "no_reliable_evidence"


def test_explicit_terminal_status_conflict_is_protocol_error() -> None:
    collector = WorkflowEventCollector()
    collector.consume(_event("message_end", status="completed"))
    collector.consume(_event("workflow_finished", status="no_reliable_evidence"))

    with pytest.raises(WorkflowProtocolError) as caught:
        collector.finalize()

    assert caught.value.code == "RAGFLOW_API_INCOMPATIBLE"
    assert caught.value.status_code == 502


def test_failed_terminal_status_cannot_override_final_completed_message() -> None:
    collector = WorkflowEventCollector()
    collector.consume(_event("message_end", status="completed"))
    collector.consume(_event("workflow_finished", status="failed"))

    with pytest.raises(WorkflowProtocolError):
        collector.finalize()


def test_duplicate_identical_terminal_frames_produce_one_stable_result() -> None:
    result = _collect(
        [
            _event("message_end", status="completed"),
            _event("workflow_finished", status="completed"),
            _event("workflow_finished", status="completed"),
        ]
    )

    assert result["data"]["status"] == "completed"


def test_node_error_can_recover_and_is_retained_as_diagnostic() -> None:
    result = _collect(
        [
            _event("node_finished", error="temporary tool error"),
            _event("message", content="恢复后的答案"),
            _event("message_end", status="completed"),
            _event("workflow_finished"),
        ]
    )

    assert result["data"]["status"] == "completed"
    assert result["data"]["_workflow_node_errors"] == ["temporary tool error"]


@pytest.mark.parametrize(
    "events, expected_error",
    [
        ([_event("user_inputs", fields=["missing-value"])], WorkflowProtocolError),
        ([{"event": "message", "data": {"content": "正文"}}], WorkflowInterrupted),
        ([_event("message_end", status="unknown"), _event("workflow_finished")], WorkflowProtocolError),
        ([_event("error", message="upstream failure")], WorkflowProtocolError),
    ],
)
def test_unsupported_or_malformed_events_never_become_success(
    events: list[dict], expected_error: type[WorkflowEventError]
) -> None:
    collector = WorkflowEventCollector()
    with pytest.raises(expected_error) as caught:
        for payload in events:
            collector.consume(payload)
        collector.finalize()

    assert caught.value.code in {"RAGFLOW_API_INCOMPATIBLE", "RUN_INTERRUPTED"}


def test_event_after_terminal_event_is_rejected() -> None:
    collector = WorkflowEventCollector()
    collector.consume(_event("message_end", status="completed"))
    collector.consume(_event("workflow_finished"))

    with pytest.raises(WorkflowProtocolError):
        collector.consume(_event("node_finished", error="late error"))
