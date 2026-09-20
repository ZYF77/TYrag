"""Protocol-level aggregation for RAGFlow Agent Workflow events.

The Gateway keeps the upstream event protocol separate from the business
message status.  A non-empty answer is never evidence that a workflow reached
its terminal event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_STATUSES = frozenset({"completed", "no_reliable_evidence", "failed"})
_CANCEL_EVENTS = frozenset(
    {"cancel", "cancelled", "canceled", "workflow_cancelled", "workflow_canceled"}
)
_CANCEL_OUTPUTS = frozenset({"task has been canceled", "task has been cancelled"})


class WorkflowEventError(Exception):
    """A deterministic upstream event protocol failure."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class WorkflowInterrupted(WorkflowEventError):
    def __init__(self, message: str = "Workflow ended before its terminal event"):
        super().__init__("RUN_INTERRUPTED", message, 503)


class WorkflowProtocolError(WorkflowEventError):
    def __init__(self, message: str = "Workflow returned an invalid event contract"):
        super().__init__("RAGFLOW_API_INCOMPATIBLE", message, 502)


def event_frame(payload: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Normalize both Agent API envelope shapes to one event/data pair."""
    event = str(payload.get("event") or "")
    session_id = payload.get("session_id") or payload.get("sessionId")
    data = payload.get("data")
    if not event and isinstance(data, dict) and data.get("event"):
        event = str(data.get("event") or "")
        session_id = session_id or data.get("session_id") or data.get("sessionId")
        inner = data.get("data")
        if isinstance(inner, dict):
            session_id = session_id or inner.get("session_id") or inner.get("sessionId")
            if "status" not in inner and "status" in data:
                inner = dict(inner)
                inner["status"] = data["status"]
        data = inner
    if not isinstance(data, dict):
        data = {}
    return event, data, str(session_id) if session_id else None


@dataclass
class WorkflowEventCollector:
    """Collect one run while preserving monotonic terminal semantics."""

    answer_parts: list[str] = field(default_factory=list)
    message_statuses: list[str] = field(default_factory=list)
    finished_statuses: list[str] = field(default_factory=list)
    reference: Any = field(default_factory=dict)
    session_id: str | None = None
    usage: Any = None
    saw_workflow_finished: bool = False
    saw_message_end: bool = False
    node_errors: list[str] = field(default_factory=list)
    saw_user_input: bool = False

    def consume(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise WorkflowProtocolError("Workflow event must be an object")
        if payload.get("code") not in (None, 0):
            raise WorkflowProtocolError("Workflow returned an error event")
        event, data, session_id = event_frame(payload)
        self.session_id = session_id or self.session_id
        if not event:
            raise WorkflowProtocolError("Workflow event is missing its event name")
        if self.saw_workflow_finished and event != "workflow_finished":
            raise WorkflowProtocolError(
                "Workflow emitted an event after workflow_finished"
            )
        if event in {"user_inputs", "user_input", "paused"}:
            self.saw_user_input = True
            raise WorkflowProtocolError("Workflow paused for user input")
        if event in _CANCEL_EVENTS:
            raise WorkflowInterrupted("Workflow was cancelled before completion")
        if event in {"error", "exception"}:
            raise WorkflowProtocolError("Workflow emitted an error event")
        if event == "node_finished":
            error = str(data.get("error") or "").strip()
            if error:
                self.node_errors.append(error[:500])
            return
        if event == "message":
            content = data.get("content") or data.get("answer")
            if content is not None:
                self.answer_parts.append(str(content))
            return
        if event == "message_end":
            self.saw_message_end = True
            message_status = data.get("status")
            if message_status is None:
                message_status = payload.get("status")
            if str(message_status or "").strip().lower() in {
                "cancel",
                "cancelled",
                "canceled",
            }:
                raise WorkflowInterrupted("Workflow was cancelled before completion")
            self._accept_status(message_status, self.message_statuses)
            self._accept_reference(data.get("reference"))
            return
        if event == "workflow_finished":
            self.saw_workflow_finished = True
            # Canvas v0.26.4 emits this exact structured output when a run is
            # cancelled after entering the workflow.  It is an execution
            # protocol marker, not a heuristic over arbitrary answer text.
            outputs = data.get("outputs")
            if isinstance(outputs, str) and outputs.strip().lower() in _CANCEL_OUTPUTS:
                raise WorkflowInterrupted("Workflow was cancelled before completion")
            finished_status = data.get("status")
            if finished_status is None:
                finished_status = payload.get("status")
            if str(finished_status or "").strip().lower() in {
                "cancel",
                "cancelled",
                "canceled",
            }:
                raise WorkflowInterrupted("Workflow was cancelled before completion")
            self._accept_status(finished_status, self.finished_statuses)
            self._accept_reference(data.get("reference"))
            self.usage = data.get("usage", self.usage)
            content = data.get("content") or data.get("answer")
            if content is not None:
                # A terminal content field is authoritative when present; the
                # stream Message events remain the fallback for older Canvas.
                self.answer_parts = [str(content)]
            return
        # started/tool/heartbeat events are useful diagnostics but carry no
        # business outcome and therefore do not affect the collector state.

    @staticmethod
    def _accept_status(value: Any, target: list[str]) -> None:
        if value is None or value == "":
            return
        status = str(value).strip()
        if status not in VALID_STATUSES:
            raise WorkflowProtocolError("Workflow status is missing or invalid")
        target.append(status)

    def _accept_reference(self, value: Any) -> None:
        if isinstance(value, (dict, list)):
            self.reference = value

    def _resolved_status(self) -> str:
        if not self.message_statuses and not self.finished_statuses:
            raise WorkflowProtocolError("Workflow terminal status is missing")
        # RAGFlow may emit more than one Message.  The last Message carrying a
        # status is authoritative; an explicit status on workflow_finished is
        # a compatibility value and must agree with that Message when present.
        message_status = self.message_statuses[-1] if self.message_statuses else None
        finished_statuses = set(self.finished_statuses)
        if len(finished_statuses) > 1:
            if "failed" not in finished_statuses:
                raise WorkflowProtocolError("Workflow terminal statuses conflict")
        finished_status = self.finished_statuses[-1] if self.finished_statuses else None
        if message_status and finished_status and message_status != finished_status:
            # A failed Message is monotonic: a later compatibility status
            # cannot restore success. The reverse direction is a protocol
            # conflict because the explicit terminal event contradicts the
            # final Message status.
            if "failed" not in self.message_statuses:
                raise WorkflowProtocolError("Workflow terminal statuses conflict")
        # A failed result is monotonic. A later usage-only or completed frame
        # cannot turn an already failed Message back into a successful one.
        if "failed" in self.message_statuses or "failed" in self.finished_statuses:
            return "failed"
        resolved = finished_status or message_status
        if resolved is None:  # defensive; the empty case is handled above
            raise WorkflowProtocolError("Workflow terminal status is missing")
        return resolved

    def partial(self) -> dict[str, Any]:
        """Return collected body/evidence without inventing a terminal state."""
        return {
            "content": "".join(self.answer_parts),
            "reference": self.reference,
        }

    def finalize(self) -> dict[str, Any]:
        if not self.saw_workflow_finished:
            raise WorkflowInterrupted()
        status = self._resolved_status()
        return {
            "event": "workflow_finished",
            "session_id": self.session_id,
            "data": {
                "content": "".join(self.answer_parts),
                "reference": self.reference,
                "status": status,
                "usage": self.usage,
                "_workflow_complete": True,
                "_workflow_node_errors": list(self.node_errors),
            },
        }
