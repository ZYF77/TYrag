"""RAGFlow → Gateway document-run terminal webhook receiver (P0).

Auth: HMAC (callback.sign_payload/verify_signature style) + intranet CIDR allowlist.
Idempotency: ragflow_status_inbox UNIQUE(event_id). Side effects share
SyncService.apply_ragflow_run with the poll path.
"""

from __future__ import annotations

import inspect

import ipaddress
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from enterprise.gateway.callback import CallbackSignatureError, verify_signature
import enterprise.gateway.config as gateway_config
from enterprise.gateway.db import GatewayDatabase
from enterprise.gateway.sync.models import (
    get_mapping_by_ragflow_document_id,
    insert_ragflow_status_inbox,
)
from enterprise.gateway.sync.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/enterprise/api/v1/internal/ragflow",
    tags=["ragflow-internal-status"],
)

EVENT_TYPE = "ragflow.document.run.terminal"
_DEFAULT_TRUSTED_CIDRS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
)


def _client_ip(request: Request) -> str:
    # Prefer direct peer; Gateway production bind is loopback / private docker net.
    # X-Forwarded-For is only consulted when the peer itself is already trusted,
    # matching the "intranet restriction" posture (no public edge trust by default).
    peer = (request.client.host if request.client else "") or ""
    if not peer:
        return ""
    if not _ip_in_cidrs(peer, _trusted_networks()):
        return peer
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or peer


def _trusted_networks() -> list[ipaddress._BaseNetwork]:
    raw = (gateway_config.config.ragflow_status_webhook_trusted_cidrs or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()] if raw else list(_DEFAULT_TRUSTED_CIDRS)
    nets: list[ipaddress._BaseNetwork] = []
    for part in parts:
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("ignoring invalid trusted CIDR %r", part)
    return nets


def _ip_in_cidrs(ip: str, nets: list[ipaddress._BaseNetwork]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def assert_intranet_client(request: Request) -> None:
    """Reject non-intranet peers (config CIDR allowlist; defaults = RFC1918+loopback)."""
    ip = _client_ip(request)
    if not ip or not _ip_in_cidrs(ip, _trusted_networks()):
        raise PermissionError(f"client {ip!r} is outside trusted intranet CIDRs")


async def get_db() -> GatewayDatabase:
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value



def _sync_service(gateway: GatewayDatabase) -> SyncService:
    import enterprise.gateway.app as app_module

    return app_module._sync_service(gateway)



def _effective_webhook_settings() -> tuple[bool, str, bool]:
    """Read post-runtime effective inbound webhook controls (not frozen boot-only)."""
    cfg = gateway_config.config
    settings = cfg.runtime_settings()
    return (
        bool(settings.ragflow_status_webhook_enabled),
        str(settings.ragflow_status_webhook_secret or ""),
        bool(settings.ragflow_status_webhook_ignore_cancel),
    )


@router.post("/document-run-terminal")
async def document_run_terminal(
    request: Request,
    gateway: GatewayDatabase = Depends(get_db),
) -> JSONResponse:
    enabled, secret, ignore_cancel = _effective_webhook_settings()
    if not enabled:
        return JSONResponse({"accepted": False, "reason": "disabled"}, status_code=503)

    try:
        assert_intranet_client(request)
    except PermissionError as exc:
        logger.warning("ragflow status webhook intranet reject: %s", exc)
        return JSONResponse({"error": "forbidden"}, status_code=403)

    raw = await request.body()
    timestamp_raw = request.headers.get("X-Enterprise-Timestamp", "")
    signature = request.headers.get("X-Enterprise-Signature", "")
    try:
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_timestamp"}, status_code=401)
    try:
        verify_signature(raw, signature, secret, timestamp)
    except CallbackSignatureError:
        return JSONResponse({"error": "invalid_signature"}, status_code=401)
    except ValueError:
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    error = _validate_envelope(body)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    event_id = str(body["eventId"])
    payload = body["payload"]
    run = str(payload.get("run") or "")
    run_code = str(payload.get("runCode") or "")
    doc_id = str(payload["ragflowDocumentId"])
    dataset_id = str(payload["ragflowDatasetId"])
    trigger = str(payload.get("trigger") or "webhook")
    occurred_at = str(body.get("occurredAt") or "")

    if ignore_cancel and run.upper() in {"CANCEL", "2"}:
        async with gateway.transaction(write=True) as conn:
            inserted = await insert_ragflow_status_inbox(
                conn,
                event_id=event_id,
                event_type=EVENT_TYPE,
                ragflow_document_id=doc_id,
                ragflow_dataset_id=dataset_id,
                run=run or "CANCEL",
                run_code=run_code or "2",
                trigger=trigger,
                occurred_at=occurred_at,
                payload_json=raw.decode("utf-8", errors="replace"),
            )
        return JSONResponse(
            {
                "accepted": True,
                "ignored": "cancel",
                "replay": not inserted,
                "eventId": event_id,
            }
        )

    async with gateway.transaction(write=True) as conn:
        inserted = await insert_ragflow_status_inbox(
            conn,
            event_id=event_id,
            event_type=EVENT_TYPE,
            ragflow_document_id=doc_id,
            ragflow_dataset_id=dataset_id,
            run=run,
            run_code=run_code,
            trigger=trigger,
            occurred_at=occurred_at,
            payload_json=raw.decode("utf-8", errors="replace"),
        )
        if not inserted:
            return JSONResponse(
                {"accepted": True, "replay": True, "eventId": event_id}
            )
        doc = await get_mapping_by_ragflow_document_id(
            conn, doc_id, ragflow_dataset_id=dataset_id
        )

    if doc is None:
        # Mapping may not be written yet; reconciler backfills. Still 200 so RF
        # does not hammer permanent failures after short retries.
        logger.info(
            "ragflow status webhook mapping missing doc=%s dataset=%s eventId=%s",
            doc_id,
            dataset_id,
            event_id,
        )
        return JSONResponse(
            {
                "accepted": True,
                "mappingFound": False,
                "eventId": event_id,
            }
        )

    try:
        await _sync_service(gateway).apply_ragflow_run(doc, run or run_code, source="webhook")
    except Exception:
        logger.exception(
            "ragflow status webhook apply failed doc=%s eventId=%s",
            doc_id,
            event_id,
        )
        return JSONResponse(
            {"accepted": False, "error": "apply_failed", "eventId": event_id},
            status_code=500,
        )

    return JSONResponse(
        {
            "accepted": True,
            "replay": False,
            "mappingFound": True,
            "eventId": event_id,
        }
    )


def _validate_envelope(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "invalid_envelope"
    for key in ("eventId", "eventType", "occurredAt", "payload"):
        if key not in body:
            return f"missing_{key}"
    if body.get("eventType") != EVENT_TYPE:
        return "invalid_event_type"
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return "invalid_payload"
    for key in ("ragflowDocumentId", "ragflowDatasetId"):
        if not str(payload.get(key) or "").strip():
            return f"missing_{key}"
    run = str(payload.get("run") or "").strip().upper()
    run_code = str(payload.get("runCode") or "").strip()
    if not run and not run_code:
        return "missing_run"
    return None
