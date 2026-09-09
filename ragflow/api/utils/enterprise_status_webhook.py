"""Enterprise Gateway document-run terminal webhook (RF-PATCH-010).

Fire-and-forget HTTP notify when a document ``run`` enters DONE/FAIL/CANCEL.
Does not talk to EAM; Gateway owns quality evaluation and EAM callbacks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "ragflow.document.run.terminal"
SCHEMA_VERSION = "rf.status.v1"
_TERMINAL_RUNS = frozenset({"DONE", "FAIL", "CANCEL", "3", "2", "4"})
_RUN_TO_CODE = {
    "UNSTART": "0",
    "RUNNING": "1",
    "CANCEL": "2",
    "DONE": "3",
    "FAIL": "4",
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}
_CODE_TO_NAME = {"0": "UNSTART", "1": "RUNNING", "2": "CANCEL", "3": "DONE", "4": "FAIL"}


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def webhook_enabled() -> bool:
    return _env_flag("ENTERPRISE_STATUS_WEBHOOK_ENABLED", "false")


def normalize_run(run: Any) -> tuple[str, str] | None:
    """Return (run_name, run_code) for a terminal run, else None."""
    if run is None:
        return None
    raw = str(run).strip().upper()
    code = _RUN_TO_CODE.get(raw)
    if code is None:
        return None
    name = _CODE_TO_NAME.get(code, raw)
    if name not in {"DONE", "FAIL", "CANCEL"}:
        return None
    return name, code


def runs_differ(old_run: Any, new_run: Any) -> bool:
    """True when the normalized run *value* changes (ignores update_time jitter)."""
    old_norm = _RUN_TO_CODE.get(str(old_run or "").strip().upper())
    new_norm = _RUN_TO_CODE.get(str(new_run or "").strip().upper())
    return old_norm != new_norm


def stable_event_id(doc_id: str, run_code: str, update_time: Any = None) -> str:
    """Stable idempotency key for a doc/run terminal transition.

    Prefer doc_id|runCode so dual FAIL hooks (task_fail + sync_progress) collapse
    to one inbox row even when update_time differs slightly. update_time is
    included only as a salt when provided and distinct retries need separation;
    P0 uses doc|run for cross-hook dedupe.
    """
    basis = f"{doc_id}|{run_code}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _sign_payload(payload: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def build_terminal_envelope(
    *,
    doc_id: str,
    kb_id: str,
    run: Any,
    progress: Any = None,
    progress_msg: str | None = None,
    trigger: str = "sync_progress",
    update_time: Any = None,
    enterprise_event_id: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_run(run)
    if normalized is None:
        return None
    run_name, run_code = normalized
    event_id = stable_event_id(doc_id, run_code, update_time)
    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    payload: dict[str, Any] = {
        "ragflowDocumentId": str(doc_id),
        "ragflowDatasetId": str(kb_id or ""),
        "run": run_name,
        "runCode": run_code,
        "trigger": trigger,
    }
    if progress is not None:
        try:
            payload["progress"] = float(progress)
        except (TypeError, ValueError):
            pass
    if progress_msg:
        payload["progressMsgTail"] = str(progress_msg)[-500:]
    if enterprise_event_id:
        payload["enterpriseEventId"] = str(enterprise_event_id)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": event_id,
        "eventType": EVENT_TYPE,
        "occurredAt": occurred_at,
        "payload": payload,
    }


def _post_once(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _deliver(url: str, secret: str, envelope: dict[str, Any], timeout_ms: int, max_attempts: int) -> None:
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timeout_s = max(0.2, timeout_ms / 1000.0)
    attempts = max(1, max_attempts)
    event_id = envelope.get("eventId", "")
    for attempt in range(1, attempts + 1):
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-Enterprise-Timestamp": str(timestamp),
            "X-Enterprise-Signature": _sign_payload(body, secret, timestamp),
            "X-Enterprise-Event-Id": str(event_id),
        }
        try:
            status = _post_once(url, body, headers, timeout_s)
        except Exception:
            logger.exception(
                "enterprise status webhook network error eventId=%s attempt=%s",
                event_id,
                attempt,
            )
            status = 0
        if 200 <= status < 300:
            return
        if status in (400, 401, 403):
            logger.error(
                "enterprise status webhook permanent failure status=%s eventId=%s",
                status,
                event_id,
            )
            return
        if attempt >= attempts:
            logger.warning(
                "enterprise status webhook exhausted retries status=%s eventId=%s",
                status,
                event_id,
            )
            return
        time.sleep(min(2.0, 0.2 * (2 ** (attempt - 1))))


def emit_document_run_terminal(
    *,
    doc_id: str,
    kb_id: str,
    old_run: Any,
    new_run: Any,
    progress: Any = None,
    progress_msg: str | None = None,
    trigger: str = "sync_progress",
    update_time: Any = None,
    enterprise_event_id: str | None = None,
    background: bool = True,
) -> bool:
    """Emit webhook if enabled and run value newly entered a terminal state."""
    if not webhook_enabled():
        return False
    if not runs_differ(old_run, new_run):
        return False
    if normalize_run(new_run) is None:
        return False
    url = os.getenv("ENTERPRISE_STATUS_WEBHOOK_URL", "").strip()
    secret = os.getenv("ENTERPRISE_STATUS_WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        logger.warning(
            "enterprise status webhook enabled but URL/secret missing; skip doc=%s",
            doc_id,
        )
        return False
    envelope = build_terminal_envelope(
        doc_id=doc_id,
        kb_id=kb_id,
        run=new_run,
        progress=progress,
        progress_msg=progress_msg,
        trigger=trigger,
        update_time=update_time,
        enterprise_event_id=enterprise_event_id,
    )
    if envelope is None:
        return False
    try:
        timeout_ms = int(os.getenv("ENTERPRISE_STATUS_WEBHOOK_TIMEOUT_MS", "2000"))
    except ValueError:
        timeout_ms = 2000
    try:
        max_attempts = int(os.getenv("ENTERPRISE_STATUS_WEBHOOK_MAX_ATTEMPTS", "3"))
    except ValueError:
        max_attempts = 3

    if background:
        threading.Thread(
            target=_deliver,
            args=(url, secret, envelope, timeout_ms, max_attempts),
            name=f"rf-status-webhook-{doc_id[:8]}",
            daemon=True,
        ).start()
    else:
        _deliver(url, secret, envelope, timeout_ms, max_attempts)
    return True


def extract_enterprise_event_id(meta_fields: Any) -> str | None:
    if not isinstance(meta_fields, dict):
        return None
    for key in ("enterprise_event_id", "enterpriseEventId", "event_id", "eventId"):
        value = meta_fields.get(key)
        if value:
            return str(value)
    return None
