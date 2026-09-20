"""Small Gateway client for the RAGFlow Agent Workflow API.

The Gateway owns the authentication and the trusted ``inputs`` envelope. The
canvas remains the execution engine; this client only transports a request and
normalizes its SSE frames. A stub is provided for the existing offline tests.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from enterprise.gateway.config import config, require_ragflow_api_key
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
)
from enterprise.gateway.query.workflow_events import (
    WorkflowEventCollector,
    WorkflowEventError,
)


class RAGFlowAgentClient(RAGFlowDocumentClient):
    """Transport for ``POST /api/v1/agents/chat/completions``."""

    @staticmethod
    def _require_ok(result: dict) -> dict:
        if not isinstance(result, dict):
            raise RAGFlowAPIError("RAGFlow Workflow returned a non-object payload", 502)
        if result.get("code") not in (0, None):
            error = RAGFlowAPIError(
                str(result.get("message") or "RAGFlow Workflow returned an error"),
                200,
            )
            data = result.get("data")
            error.diagnostics = data.get("_diagnostics") if isinstance(data, dict) else None
            raise error
        return result

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Parent ``RAGFlowDocumentClient.__init__`` assigns ``self.timeout`` from
        # ``ragflow_timeout``. Our ``@timeout.setter`` no-op absorbs that write so
        # construction does not AttributeError, and the getter below keeps reading
        # hot-reloadable ``workflow_timeout_seconds``.
        super().__init__(
            base_url=base_url or config.ragflow_base_url,
            api_key=api_key or require_ragflow_api_key(),
        )

    @property
    def timeout(self) -> float:
        """Hot-reloadable Agent Workflow HTTP timeout (runtime snapshot)."""
        snap = config.runtime_settings()
        return float(
            getattr(snap, "workflow_timeout_seconds", None)
            or getattr(config, "workflow_timeout", 120.0)
            or 120.0
        )

    @timeout.setter
    def timeout(self, _value: float) -> None:
        """No-op: parent ``__init__`` writes timeout; keep property-backed value."""
        return

    @staticmethod
    def _body(
        *,
        agent_id: str,
        question: str,
        session_id: str | None,
        user_id: str,
        inputs: dict[str, Any],
        files: list[dict[str, Any]],
        stream: bool,
        return_trace: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "query": question,
            "stream": bool(stream),
            "user_id": user_id,
            "inputs": dict(inputs),
        }
        if session_id:
            body["session_id"] = session_id
        if files:
            body["files"] = list(files)
        if return_trace:
            body["return_trace"] = True
        return body

    async def complete(
        self,
        *,
        agent_id: str,
        question: str,
        session_id: str | None,
        user_id: str,
        inputs: dict[str, Any],
        files: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        return_trace: bool = False,
    ) -> dict:
        rid = request_id or self._new_request_id()
        # The Agent API's JSON response is assembled from message_end and can
        # omit workflow_finished. Consume the same SSE protocol used by the
        # public streaming route so JSON and SSE share terminal semantics.
        collector = WorkflowEventCollector()
        try:
            async for payload in self.stream(
                agent_id=agent_id,
                question=question,
                session_id=session_id,
                user_id=user_id,
                inputs=inputs,
                files=files,
                request_id=rid,
                return_trace=return_trace,
            ):
                collector.consume(payload)
            # Keep the Agent client's existing JSON envelope while deriving it
            # from the same collected SSE terminal contract.
            return {"code": 0, "data": collector.finalize()}
        except WorkflowEventError as exc:
            error = RAGFlowAPIError(exc.message, exc.status_code, rid)
            error.code = exc.code
            error.workflow_partial = collector.partial()
            raise error from exc
        except RAGFlowAPIError as exc:
            exc.workflow_partial = collector.partial()
            raise

    async def stream(
        self,
        *,
        agent_id: str,
        question: str,
        session_id: str | None,
        user_id: str,
        inputs: dict[str, Any],
        files: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        return_trace: bool = False,
    ) -> AsyncIterator[dict]:
        import httpx

        rid = request_id or self._new_request_id()
        body = self._body(
            agent_id=agent_id,
            question=question,
            session_id=session_id,
            user_id=user_id,
            inputs=inputs,
            files=files or [],
            stream=True,
            return_trace=return_trace,
        )
        timeout = httpx.Timeout(self.timeout, connect=self.timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/v1/agents/chat/completions",
                    json=body,
                    headers=self._headers(rid),
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise RAGFlowAPIError(
                            "RAGFlow Workflow stream request failed",
                            response.status_code,
                            rid,
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload_text = line[len("data:") :].strip()
                        if not payload_text or payload_text == "[DONE]":
                            continue
                        try:
                            payload = json.loads(payload_text)
                        except json.JSONDecodeError as exc:
                            raise RAGFlowAPIError(
                                "RAGFlow Workflow returned malformed SSE", 502, rid
                            ) from exc
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("code") not in (0, None):
                            try:
                                self._require_ok(payload)
                            except RAGFlowAPIError as exc:
                                exc.request_id = rid
                                raise
                        yield payload
        except httpx.HTTPError as exc:
            raise RAGFlowAPIError("RAGFlow Workflow request failed", 0, rid) from exc


class RAGFlowAgentStub:
    """Deterministic Workflow transport for Gateway contract tests."""

    def __init__(self):
        self.answer = "Workflow test answer [ID:0]"
        self.status = "completed"
        self.reference: dict[str, Any] = {"chunks": [], "doc_aggs": []}
        self.session_id = f"workflow-session-{uuid.uuid4().hex[:10]}"
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> dict:
        self.complete_calls.append(dict(kwargs))
        collector = WorkflowEventCollector()
        async for payload in self.stream(**kwargs):
            collector.consume(payload)
        return {"code": 0, "data": collector.finalize()}

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        self.stream_calls.append(dict(kwargs))
        yield {
            "event": "workflow_started",
            "session_id": self.session_id,
            "data": {},
        }
        yield {
            "event": "message",
            "session_id": self.session_id,
            "data": {"content": self.answer},
        }
        yield {
            "event": "message_end",
            "session_id": self.session_id,
            "data": {"reference": self.reference},
        }
        yield {
            "event": "workflow_finished",
            "session_id": self.session_id,
            "data": {"status": self.status},
        }
