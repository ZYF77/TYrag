"""Request-scoped, best-effort diagnostics for enterprise RAG calls.

The sink deliberately accepts only bounded structured metadata.  It never
stores prompts, chunk bodies, raw model output, or tool payloads, and callers
must not depend on it for business behaviour.
"""

from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Protocol


_MAX_EVENTS = 256
_MAX_LIST_ITEMS = 64
_MAX_TRACE_BYTES = 256 * 1024
_MAX_QUERY_CHARS = 1024
_MAX_STRING_CHARS = 512
_BLOCKED_KEYS = {
    "answer",
    "body",
    "content",
    "contentltks",
    "contentwithweight",
    "effectiveknowledge",
    "history",
    "knowledge",
    "messages",
    "output",
    "prompt",
    "raw",
    "response",
    "system",
    "text",
    "toolargs",
    "toolresult",
}


class RagDiagnosticsSink(Protocol):
    """The only diagnostics abstraction used by the RAG business path."""

    def record(self, event_type: str, payload: dict[str, Any]) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


class NoopDiagnosticsSink:
    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        del event_type, payload

    def snapshot(self) -> dict[str, Any]:
        return {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_key(key: Any) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> tuple[Any, bool]:
    if depth > 5:
        return None, True
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None, False
        return value, False
    if isinstance(value, str):
        limit = _MAX_QUERY_CHARS if _safe_key(key) in {"query", "question"} else _MAX_STRING_CHARS
        return value[:limit], len(value) > limit
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        truncated = len(items) > _MAX_LIST_ITEMS
        sanitized = []
        for item in items[:_MAX_LIST_ITEMS]:
            clean, child_truncated = _sanitize(item, key=key, depth=depth + 1)
            sanitized.append(clean)
            truncated = truncated or child_truncated
        return sanitized, truncated
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        truncated = False
        for raw_key, raw_value in value.items():
            normalized = _safe_key(raw_key)
            if normalized in _BLOCKED_KEYS:
                truncated = True
                continue
            clean, child_truncated = _sanitize(
                raw_value, key=str(raw_key), depth=depth + 1
            )
            result[str(raw_key)] = clean
            truncated = truncated or child_truncated
        return result, truncated
    return str(value)[:_MAX_STRING_CHARS], True


class InMemoryDiagnosticsSink:
    def __init__(self, run_id: str):
        self._run_id = str(run_id)[:128]
        self._started_at = _utc_now()
        self._started = time.perf_counter()
        self._events: list[dict[str, Any]] = []
        self._bytes = 0
        self._truncated = False
        self._lock = threading.Lock()

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        clean, truncated = _sanitize(payload)
        event = {
            "type": str(event_type)[:64],
            "atMs": round((time.perf_counter() - self._started) * 1000, 3),
            "data": clean if isinstance(clean, dict) else {},
        }
        encoded_size = len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        with self._lock:
            self._truncated = self._truncated or truncated
            if len(self._events) >= _MAX_EVENTS or self._bytes + encoded_size > _MAX_TRACE_BYTES:
                self._truncated = True
                return
            self._events.append(event)
            self._bytes += encoded_size

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            truncated = self._truncated
        return {
            "version": 1,
            "runId": self._run_id,
            "startedAt": self._started_at,
            "durationMs": round((time.perf_counter() - self._started) * 1000, 3),
            "events": events,
            "truncated": truncated,
        }


_NOOP = NoopDiagnosticsSink()
_CURRENT_SINK: ContextVar[RagDiagnosticsSink] = ContextVar(
    "rag_diagnostics_sink", default=_NOOP
)


def begin_rag_diagnostics(enabled: bool, run_id: str) -> Token:
    try:
        sink: RagDiagnosticsSink = (
            InMemoryDiagnosticsSink(run_id)
            if enabled and str(run_id).strip()
            else _NOOP
        )
    except Exception:
        sink = _NOOP
    return _CURRENT_SINK.set(sink)


def reset_rag_diagnostics(token: Token) -> None:
    try:
        _CURRENT_SINK.reset(token)
    except Exception:
        pass


def record_rag_diagnostics(event_type: str, payload: dict[str, Any]) -> None:
    try:
        _CURRENT_SINK.get().record(event_type, payload)
    except Exception:
        pass


def snapshot_rag_diagnostics() -> dict[str, Any]:
    try:
        return _CURRENT_SINK.get().snapshot()
    except Exception:
        return {}
