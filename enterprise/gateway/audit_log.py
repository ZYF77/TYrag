"""JSONL audit logs for FILE_SHARE register and callback traffic.

Logs are written to the Gateway state volume, not the container writable layer.
HMAC secrets, passwords, and tokens are never persisted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_HTTP_LOCK = threading.Lock()
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_RESPONSE_LIMIT = 2000
_HTTP_EVENT_LIMIT = 200
_HTTP_EVENTS: deque[dict[str, Any]] = deque(maxlen=_HTTP_EVENT_LIMIT)
_HTTP_SEQ = 0
_REDACT_KEYS = {
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "hmac",
    "private_key",
    "shared_secret",
}

_TEXT_KEYS = {
    "content",
    "answer",
    "text",
    "message",
    "prompt",
    "completion",
    "delta",
    "question",
}
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def _looks_like_binary_payload(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith("data:") and ";base64," in stripped[:80]:
        return True
    return len(stripped) >= 32 and bool(_B64_RE.fullmatch(stripped))


def _truncate_text(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= _RESPONSE_LIMIT:
            return value
        return value[:_RESPONSE_LIMIT] + f"...<truncated {len(value) - _RESPONSE_LIMIT} chars>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "content" and isinstance(item, str) and _looks_like_binary_payload(item):
                out[key] = "<redacted>"
            elif lowered in _TEXT_KEYS and isinstance(item, str):
                out[key] = _truncate_text(item)
            else:
                out[key] = _truncate_text(item)
        return out
    if isinstance(value, list):
        return [_truncate_text(item) for item in value]
    return value


def audit_log_dir() -> Path:
    override = (os.environ.get("ENTERPRISE_AUDIT_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    db_path = (
        os.environ.get("ENTERPRISE_SYNC_DB_PATH")
        or os.environ.get("ENTERPRISE_DB_PATH")
        or ""
    ).strip()
    if db_path:
        return Path(db_path).resolve().parent / "logs"
    return Path("/var/lib/tyrag/state/logs")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _REDACT_KEYS):
                out[key] = "<redacted>"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in (headers or {}).items():
        lowered = key.lower()
        if any(part in lowered for part in _REDACT_KEYS):
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def _parse_body(raw: str | bytes | dict | None) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return _truncate_text(_redact(raw))
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    text = text.strip()
    if not text:
        return None
    try:
        return _truncate_text(_redact(json.loads(text)))
    except json.JSONDecodeError:
        return {"_raw": text[:_RESPONSE_LIMIT], "_truncated": len(text) > _RESPONSE_LIMIT}


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _MAX_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{_BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()
    for index in range(_BACKUP_COUNT - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            src.replace(dst)
    path.replace(path.with_name(f"{path.name}.1"))


def _audit_enabled() -> bool:
    if (os.environ.get("ENTERPRISE_AUDIT_LOG_DIR") or "").strip():
        return True
    return os.environ.get("ENTERPRISE_TEST_MODE") != "1"


def write_audit_event(filename: str, record: dict[str, Any]) -> None:
    if not _audit_enabled():
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **_redact(record),
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        directory = audit_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        with _LOCK:
            _rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        logger.exception("audit log write failed file=%s", filename)


def record_http_event(record: dict[str, Any]) -> dict[str, Any]:
    """Keep a redacted in-memory ring of gateway HTTP traffic for the harness."""
    global _HTTP_SEQ
    payload = _redact(
        {
            "direction": "inbound",
            **record,
            "ts": record.get("ts") or datetime.now(timezone.utc).isoformat(),
        }
    )
    with _HTTP_LOCK:
        _HTTP_SEQ += 1
        payload["id"] = str(_HTTP_SEQ)
        _HTTP_EVENTS.append(payload)
    return payload


def list_http_events(limit: int = 100) -> list[dict[str, Any]]:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = 100
    parsed = max(1, min(parsed, _HTTP_EVENT_LIMIT))
    with _HTTP_LOCK:
        items = list(_HTTP_EVENTS)
    items.reverse()
    return items[:parsed]


def clear_http_events() -> None:
    global _HTTP_SEQ
    with _HTTP_LOCK:
        _HTTP_EVENTS.clear()
        _HTTP_SEQ = 0


def write_feed_register_audit(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Any,
    http_status: int,
) -> None:
    write_audit_event(
        "feed-register.jsonl",
        {
            "kind": "feed.register.inbound",
            "method": method,
            "path": path,
            "headers": _safe_headers(headers),
            "body": _parse_body(body),
            "http_status": http_status,
        },
    )


def write_feed_callback_audit(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
    http_status: int | None,
    response_body: str | None,
    delivery_id: str | None,
    outcome: str,
    error: str | None = None,
) -> None:
    response: Any = None
    if response_body:
        response = _parse_body(response_body)
        if isinstance(response, dict) and response.get("_raw"):
            pass
        elif isinstance(response_body, str) and len(response_body) > _RESPONSE_LIMIT:
            response = _parse_body(response_body[:_RESPONSE_LIMIT])
    write_audit_event(
        "feed-callback.jsonl",
        {
            "kind": "feed.callback.outbound",
            "method": method,
            "url": url,
            "headers": _safe_headers(headers),
            "body": _parse_body(body),
            "http_status": http_status,
            "response_body": response,
            "delivery_id": delivery_id,
            "outcome": outcome,
            "error": error,
        },
    )
    record_http_event(
        {
            "kind": "feed.callback.outbound",
            "direction": "outbound",
            "method": method,
            "path": url,
            "query": "",
            "body": _parse_body(body),
            "http_status": http_status,
            "response_body": response,
            "streamed": False,
            "outcome": outcome,
            "error": error,
        }
    )


def write_inquiry_audit(
    *,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body: Any,
    http_status: int,
    response_body: Any,
    streamed: bool = False,
) -> None:
    write_audit_event(
        "inquiry.jsonl",
        {
            "kind": "inquiry.http",
            "method": method,
            "path": path,
            "query": query,
            "headers": _safe_headers(headers),
            "body": _parse_body(body),
            "http_status": http_status,
            "response_body": _parse_body(response_body) if not streamed else response_body,
            "streamed": streamed,
        },
    )


def configure_gateway_file_logging() -> Path | None:
    """Send enterprise.gateway logs to the state volume."""
    if not _audit_enabled():
        return None
    try:
        directory = audit_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "gateway.log"
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        package_logger = logging.getLogger("enterprise.gateway")
        package_logger.setLevel(logging.INFO)
        already = False
        for existing in package_logger.handlers:
            if isinstance(existing, logging.FileHandler) and getattr(
                existing, "baseFilename", ""
            ) == str(path):
                already = True
                break
        if not already:
            package_logger.addHandler(handler)
            package_logger.propagate = True
        return directory
    except Exception:
        logger.exception("gateway file logging setup failed")
        return None
