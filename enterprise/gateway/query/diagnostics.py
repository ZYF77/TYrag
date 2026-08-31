"""Bounded private diagnostics stored with an existing v2 message run."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any


_MAX_EVENTS = 256
_MAX_LIST_ITEMS = 128
_MAX_TRACE_BYTES = 256 * 1024
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _key(value: Any) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _clean(value: Any, *, key: str = "", depth: int = 0) -> tuple[Any, bool]:
    if depth > 5:
        return None, True
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None, False
        return value, False
    if isinstance(value, str):
        limit = 1024 if _key(key) in {"query", "question"} else 512
        return value[:limit], len(value) > limit
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = []
        truncated = len(items) > _MAX_LIST_ITEMS
        for item in items[:_MAX_LIST_ITEMS]:
            clean, child_truncated = _clean(item, key=key, depth=depth + 1)
            out.append(clean)
            truncated = truncated or child_truncated
        return out, truncated
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        truncated = False
        for raw_key, raw_value in value.items():
            if _key(raw_key) in _BLOCKED_KEYS:
                truncated = True
                continue
            clean, child_truncated = _clean(
                raw_value, key=str(raw_key), depth=depth + 1
            )
            out[str(raw_key)] = clean
            truncated = truncated or child_truncated
        return out, truncated
    return str(value)[:512], True


def start_trace(
    run_id: str,
    *,
    query: str,
    reasoning_mode: str,
    stream: bool,
) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "version": 1,
        "runId": str(run_id),
        "startedAt": _utc_now(),
        "events": [],
        "truncated": False,
        "_startedMonotonic": time.perf_counter(),
        "_bytes": 0,
    }
    record_event(
        trace,
        "request",
        {
            "source": "gateway",
            "query": query,
            "reasoningMode": reasoning_mode,
            "stream": stream,
        },
    )
    return trace


def record_event(trace: dict[str, Any] | None, event_type: str, data: dict[str, Any]) -> None:
    if not isinstance(trace, dict):
        return
    try:
        clean, truncated = _clean(data)
        started = float(trace.get("_startedMonotonic") or time.perf_counter())
        event = {
            "type": str(event_type)[:64],
            "atMs": round((time.perf_counter() - started) * 1000, 3),
            "data": clean if isinstance(clean, dict) else {},
        }
        size = len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        events = trace.setdefault("events", [])
        used = int(trace.get("_bytes") or 0)
        trace["truncated"] = bool(trace.get("truncated")) or truncated
        if len(events) >= _MAX_EVENTS or used + size > _MAX_TRACE_BYTES:
            trace["truncated"] = True
            return
        events.append(event)
        trace["_bytes"] = used + size
    except Exception:
        return


def merge_upstream(trace: dict[str, Any] | None, upstream: Any) -> None:
    if not isinstance(trace, dict) or not isinstance(upstream, dict):
        return
    try:
        if str(upstream.get("runId") or "") != str(trace.get("runId") or ""):
            return
        for event in upstream.get("events") or []:
            if not isinstance(event, dict):
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            record_event(trace, str(event.get("type") or "upstream"), {"source": "ragflow", **data})
        trace["truncated"] = bool(trace.get("truncated")) or bool(
            upstream.get("truncated")
        )
    except Exception:
        return


def finish_trace(
    trace: dict[str, Any] | None,
    *,
    outcome: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    if not isinstance(trace, dict):
        return {}
    try:
        data: dict[str, Any] = {"source": "gateway", "outcome": outcome}
        if error_code:
            data["errorCode"] = error_code
        record_event(trace, "outcome", data)
        started = float(trace.pop("_startedMonotonic", time.perf_counter()))
        trace.pop("_bytes", None)
        trace["durationMs"] = round((time.perf_counter() - started) * 1000, 3)
        return trace
    except Exception:
        return {}
