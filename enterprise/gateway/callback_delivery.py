"""Outbound FILE_SHARE terminal callback delivery (outbox + worker).

Gateway polls RAGFlow separately; this module only pushes terminal results
to server-configured device-system endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import aiosqlite
import httpx

from enterprise.gateway.audit_log import write_feed_callback_audit
from enterprise.gateway.callback import classify_delivery, sign_payload
from enterprise.gateway.sync.models import ExtDocumentMap, utc_now

logger = logging.getLogger(__name__)

TerminalStatus = Literal["retrievable", "failed", "review_required"]
DeliveryState = Literal["pending", "delivered", "dead_letter"]

CALLBACK_EVENT_TYPE = "document.terminal"
CALLBACK_PAYLOAD_VERSION = "1"

# Internal fixtures must never POST to device-system (EAM) endpoints.
_INTERNAL_CALLBACK_ID_PREFIXES = ("PROBE-", "TYRAG-E2E-")


def is_internal_callback_document(external_document_id: str | None) -> bool:
    doc_id = (external_document_id or "").strip().upper()
    return any(doc_id.startswith(prefix) for prefix in _INTERNAL_CALLBACK_ID_PREFIXES)

# Freeze schedule: 1/5/30/120/600… then hold at 600s through attempt 8.
RETRY_DELAY_SECONDS = (1, 5, 30, 120, 600, 600, 600, 600)

CREATE_CALLBACK_DELIVERY = """
CREATE TABLE IF NOT EXISTS callback_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    originating_event_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    terminal_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    next_attempt_at TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    last_http_status INTEGER,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(
        tenant_id, source_system, external_document_id,
        source_version_id, terminal_status
    )
);

CREATE INDEX IF NOT EXISTS idx_callback_delivery_pending
    ON callback_delivery(state, next_attempt_at);
"""


@dataclass(frozen=True)
class CallbackEndpoint:
    url: str
    secret: str
    key_id: str | None = None


@dataclass
class CallbackDelivery:
    id: int | None
    delivery_id: str
    originating_event_id: str
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    terminal_status: str
    payload_json: str
    payload_hash: str
    endpoint_url: str
    attempts: int
    max_attempts: int
    next_attempt_at: str | None
    state: str
    last_http_status: int | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def retry_delay_seconds(attempt: int) -> float:
    if attempt < 1:
        raise ValueError("attempt must be positive")
    index = min(attempt - 1, len(RETRY_DELAY_SECONDS) - 1)
    return float(RETRY_DELAY_SECONDS[index])


def parse_callback_endpoints(
    raw: str | None,
    *,
    default_secret: str = "",
) -> dict[str, CallbackEndpoint]:
    """Parse ENTERPRISE_CALLBACK_ENDPOINTS JSON.

    Accepted shapes:
      {"EAM": "https://eam.example/callback"}
      {"EAM": {"url": "https://...", "secret": "...", "keyId": "..."}}
      {"tenant-a|EAM": "https://..."}
    """
    text = (raw or "").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("ENTERPRISE_CALLBACK_ENDPOINTS must be a JSON object")
    endpoints: dict[str, CallbackEndpoint] = {}
    for key, value in data.items():
        binding = str(key).strip()
        if not binding:
            raise ValueError("callback endpoint binding key is required")
        if isinstance(value, str):
            url = value.strip()
            secret = default_secret
            key_id = None
        elif isinstance(value, dict):
            url = str(value.get("url") or "").strip()
            secret = str(value.get("secret") or default_secret).strip()
            key_id_raw = value.get("keyId") or value.get("key_id")
            key_id = str(key_id_raw).strip() if key_id_raw else None
        else:
            raise ValueError(f"unsupported callback endpoint value for {binding}")
        if not url:
            raise ValueError(f"callback endpoint URL is required for {binding}")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"callback endpoint URL is invalid for {binding}")
        endpoints[binding] = CallbackEndpoint(url=url, secret=secret, key_id=key_id)
    return endpoints


def resolve_callback_endpoint(
    endpoints: dict[str, CallbackEndpoint],
    *,
    tenant_id: str,
    source_system: str,
) -> CallbackEndpoint | None:
    for key in (f"{tenant_id}|{source_system}", source_system):
        endpoint = endpoints.get(key)
        if endpoint is not None:
            return endpoint
    return None


def _localize_callback_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose Chinese user-facing messages to EAM; keep code/retryable/reasonCodes."""
    if not error:
        return None
    from enterprise.gateway.app import safe_error_message

    code = str(error.get("code") or "").strip()
    raw_message = str(error.get("message") or "").strip()
    localized: dict[str, Any] = {
        "code": code,
        "message": safe_error_message(code, raw_message or "请求失败，请稍后重试。"),
        "retryable": bool(error.get("retryable")),
    }
    reason_codes = error.get("reasonCodes")
    if isinstance(reason_codes, list):
        cleaned = [
            str(item).strip()
            for item in reason_codes
            if str(item).strip()
        ][:32]
        if cleaned:
            localized["reasonCodes"] = cleaned
    return localized


def build_terminal_payload(
    *,
    delivery_id: str,
    originating_event_id: str,
    doc: ExtDocumentMap,
    terminal_status: TerminalStatus,
    quality_status: str | None,
    retrievable: bool,
    error: dict[str, Any] | None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "deliveryId": delivery_id,
        "eventType": CALLBACK_EVENT_TYPE,
        "originatingEventId": originating_event_id,
        "externalDocumentId": doc.external_document_id,
        "sourceVersionId": doc.source_version_id,
        "status": terminal_status,
        "timestamp": timestamp or utc_now(),
        "payloadVersion": CALLBACK_PAYLOAD_VERSION,
        "tenantId": doc.tenant_id,
        "sourceSystem": doc.source_system,
        "qualityStatus": quality_status,
        "retrievable": bool(retrievable),
        "error": _localize_callback_error(error),
    }


async def ensure_callback_delivery_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(CREATE_CALLBACK_DELIVERY)
    await db.commit()


def _row_to_delivery(row: aiosqlite.Row) -> CallbackDelivery:
    return CallbackDelivery(
        id=row["id"],
        delivery_id=row["delivery_id"],
        originating_event_id=row["originating_event_id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        terminal_status=row["terminal_status"],
        payload_json=row["payload_json"],
        payload_hash=row["payload_hash"],
        endpoint_url=row["endpoint_url"],
        attempts=int(row["attempts"] or 0),
        max_attempts=int(row["max_attempts"] or 8),
        next_attempt_at=row["next_attempt_at"],
        state=row["state"],
        last_http_status=row["last_http_status"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_callback_delivery(
    db: aiosqlite.Connection,
    *,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    terminal_status: str,
) -> CallbackDelivery | None:
    async with db.execute(
        """SELECT * FROM callback_delivery
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
             AND source_version_id=? AND terminal_status=?""",
        (
            tenant_id,
            source_system,
            external_document_id,
            source_version_id,
            terminal_status,
        ),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_delivery(row) if row else None


async def enqueue_terminal_callback(
    db: aiosqlite.Connection,
    *,
    doc: ExtDocumentMap,
    terminal_status: TerminalStatus,
    quality_status: str | None = None,
    retrievable: bool = False,
    error: dict[str, Any] | None = None,
    endpoints: dict[str, CallbackEndpoint] | None = None,
    default_secret: str | None = None,
    max_attempts: int | None = None,
    enabled: bool | None = None,
) -> CallbackDelivery | None:
    """Idempotently enqueue one terminal callback. Never raises to callers."""
    from enterprise.gateway.config import config

    try:
        if enabled is None:
            enabled = config.callback_enabled
        if not enabled:
            return None
        if is_internal_callback_document(doc.external_document_id):
            logger.info(
                "skip internal fixture callback external_document_id=%s "
                "terminal_status=%s",
                doc.external_document_id,
                terminal_status,
            )
            return None
        endpoint_map = endpoints
        if endpoint_map is None:
            endpoint_map = parse_callback_endpoints(
                os.environ.get("ENTERPRISE_CALLBACK_ENDPOINTS"),
                default_secret=(
                    default_secret
                    if default_secret is not None
                    else config.callback_hmac_secret
                ),
            )
        endpoint = resolve_callback_endpoint(
            endpoint_map,
            tenant_id=doc.tenant_id,
            source_system=doc.source_system,
        )
        if endpoint is None:
            logger.info(
                "callback endpoint not configured tenant_id=%s source_system=%s "
                "external_document_id=%s terminal_status=%s",
                doc.tenant_id,
                doc.source_system,
                doc.external_document_id,
                terminal_status,
            )
            return None
        if not endpoint.secret:
            logger.warning(
                "callback secret missing for source_system=%s; skip enqueue",
                doc.source_system,
            )
            return None

        attempts_limit = (
            max_attempts if max_attempts is not None else config.callback_max_attempts
        )
        delivery_id = str(uuid.uuid4())
        payload = build_terminal_payload(
            delivery_id=delivery_id,
            originating_event_id=doc.event_id,
            doc=doc,
            terminal_status=terminal_status,
            quality_status=quality_status,
            retrievable=retrievable,
            error=error,
        )
        payload_json = _canonical_json(payload).decode("utf-8")
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = utc_now()
        try:
            await db.execute(
                """INSERT INTO callback_delivery (
                    delivery_id, originating_event_id, tenant_id, source_system,
                    external_document_id, source_version_id, terminal_status,
                    payload_json, payload_hash, endpoint_url, attempts,
                    max_attempts, next_attempt_at, state, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'pending', ?, ?)""",
                (
                    delivery_id,
                    doc.event_id,
                    doc.tenant_id,
                    doc.source_system,
                    doc.external_document_id,
                    doc.source_version_id,
                    terminal_status,
                    payload_json,
                    payload_hash,
                    endpoint.url,
                    attempts_limit,
                    now,
                    now,
                    now,
                ),
            )
            await db.commit()
        except Exception:
            # Unique constraint → already enqueued for this terminal status.
            await db.rollback()
            existing = await get_callback_delivery(
                db,
                tenant_id=doc.tenant_id,
                source_system=doc.source_system,
                external_document_id=doc.external_document_id,
                source_version_id=doc.source_version_id,
                terminal_status=terminal_status,
            )
            return existing

        return await get_callback_delivery(
            db,
            tenant_id=doc.tenant_id,
            source_system=doc.source_system,
            external_document_id=doc.external_document_id,
            source_version_id=doc.source_version_id,
            terminal_status=terminal_status,
        )
    except Exception:
        logger.exception(
            "callback enqueue failed tenant_id=%s external_document_id=%s "
            "terminal_status=%s",
            getattr(doc, "tenant_id", None),
            getattr(doc, "external_document_id", None),
            terminal_status,
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def claim_pending_callback_deliveries(
    db: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[CallbackDelivery]:
    now = utc_now()
    async with db.execute(
        """SELECT * FROM callback_delivery
           WHERE state='pending'
             AND (next_attempt_at IS NULL OR next_attempt_at<=?)
           ORDER BY next_attempt_at ASC, id ASC
           LIMIT ?""",
        (now, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_delivery(row) for row in rows]


async def mark_callback_delivered(
    db: aiosqlite.Connection,
    delivery: CallbackDelivery,
    *,
    http_status: int,
) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE callback_delivery
           SET state='delivered', attempts=?, last_http_status=?,
               last_error_code=NULL, last_error_message=NULL,
               next_attempt_at=NULL, updated_at=?
           WHERE delivery_id=?""",
        (delivery.attempts + 1, http_status, now, delivery.delivery_id),
    )
    await db.commit()


async def mark_callback_retry(
    db: aiosqlite.Connection,
    delivery: CallbackDelivery,
    *,
    http_status: int | None,
    error_code: str,
    error_message: str,
    delay_seconds: float,
) -> None:
    now_dt = datetime.now(timezone.utc)
    next_at = (now_dt + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
    await db.execute(
        """UPDATE callback_delivery
           SET state='pending', attempts=?, next_attempt_at=?,
               last_http_status=?, last_error_code=?, last_error_message=?,
               updated_at=?
           WHERE delivery_id=?""",
        (
            delivery.attempts + 1,
            next_at,
            http_status,
            error_code,
            error_message[:500],
            utc_now(),
            delivery.delivery_id,
        ),
    )
    await db.commit()


async def mark_callback_dead_letter(
    db: aiosqlite.Connection,
    delivery: CallbackDelivery,
    *,
    http_status: int | None,
    error_code: str,
    error_message: str,
) -> None:
    await db.execute(
        """UPDATE callback_delivery
           SET state='dead_letter', attempts=?, last_http_status=?,
               last_error_code=?, last_error_message=?,
               next_attempt_at=NULL, updated_at=?
           WHERE delivery_id=?""",
        (
            delivery.attempts + 1,
            http_status,
            error_code,
            error_message[:500],
            utc_now(),
            delivery.delivery_id,
        ),
    )
    await db.commit()


class CallbackDeliveryWorker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        endpoints: dict[str, CallbackEndpoint] | None = None,
        default_secret: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        from enterprise.gateway.config import config

        self.db = db
        self._endpoints = endpoints
        self._default_secret = (
            default_secret
            if default_secret is not None
            else config.callback_hmac_secret
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout_seconds = timeout_seconds

    def _endpoint_for(self, delivery: CallbackDelivery) -> CallbackEndpoint | None:
        endpoints = self._endpoints
        if endpoints is None:
            endpoints = parse_callback_endpoints(
                os.environ.get("ENTERPRISE_CALLBACK_ENDPOINTS"),
                default_secret=self._default_secret,
            )
        resolved = resolve_callback_endpoint(
            endpoints,
            tenant_id=delivery.tenant_id,
            source_system=delivery.source_system,
        )
        if resolved is not None:
            return resolved
        # Fall back to the URL stored at enqueue time with the default secret.
        if delivery.endpoint_url and self._default_secret:
            return CallbackEndpoint(
                url=delivery.endpoint_url,
                secret=self._default_secret,
            )
        return None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def deliver_one(self, delivery: CallbackDelivery) -> str:
        endpoint = self._endpoint_for(delivery)
        if endpoint is None or not endpoint.secret:
            await mark_callback_dead_letter(
                self.db,
                delivery,
                http_status=None,
                error_code="CALLBACK_ENDPOINT_UNCONFIGURED",
                error_message="Callback endpoint or secret is not configured",
            )
            write_feed_callback_audit(
                method="POST",
                url="",
                headers={},
                body=delivery.payload_json,
                http_status=None,
                response_body=None,
                delivery_id=delivery.delivery_id,
                outcome="dead_letter",
                error="Callback endpoint or secret is not configured",
            )
            return "dead_letter"

        body = delivery.payload_json.encode("utf-8")
        timestamp = int(time.time())
        signature = sign_payload(body, endpoint.secret, timestamp)
        headers = {
            "Content-Type": "application/json",
            "X-TY-Timestamp": str(timestamp),
            "X-TY-Signature": signature,
        }
        if endpoint.key_id:
            headers["X-TY-Key-Id"] = endpoint.key_id

        attempt = delivery.attempts + 1
        http_status: int | None = None
        response_text = ""
        outcome = "dead_letter"
        error_text: str | None = None
        try:
            client = await self._ensure_client()
            response = await client.post(
                endpoint.url,
                content=body,
                headers=headers,
            )
            http_status = int(response.status_code)
            response_text = response.text[:2000]
        except Exception as exc:
            error_text = str(exc) or "transport error"
            decision = classify_delivery(
                503,
                attempt=attempt,
                max_attempts=delivery.max_attempts,
            )
            if decision.status == "retry_wait":
                await mark_callback_retry(
                    self.db,
                    delivery,
                    http_status=None,
                    error_code="CALLBACK_TRANSPORT_ERROR",
                    error_message=error_text,
                    delay_seconds=retry_delay_seconds(attempt),
                )
                outcome = "retry_wait"
            else:
                await mark_callback_dead_letter(
                    self.db,
                    delivery,
                    http_status=None,
                    error_code="CALLBACK_TRANSPORT_ERROR",
                    error_message=error_text,
                )
                outcome = "dead_letter"
            write_feed_callback_audit(
                method="POST",
                url=endpoint.url,
                headers=headers,
                body=delivery.payload_json,
                http_status=http_status,
                response_body=response_text,
                delivery_id=delivery.delivery_id,
                outcome=outcome,
                error=error_text,
            )
            return outcome

        decision = classify_delivery(
            http_status,
            attempt=attempt,
            max_attempts=delivery.max_attempts,
        )
        if decision.status == "delivered":
            await mark_callback_delivered(
                self.db, delivery, http_status=http_status
            )
            outcome = "delivered"
        elif decision.status == "retry_wait":
            await mark_callback_retry(
                self.db,
                delivery,
                http_status=http_status,
                error_code="CALLBACK_HTTP_RETRYABLE",
                error_message=f"HTTP {http_status}",
                delay_seconds=retry_delay_seconds(attempt),
            )
            outcome = "retry_wait"
        else:
            await mark_callback_dead_letter(
                self.db,
                delivery,
                http_status=http_status,
                error_code="CALLBACK_HTTP_PERMANENT",
                error_message=f"HTTP {http_status}",
            )
            outcome = "dead_letter"
        write_feed_callback_audit(
            method="POST",
            url=endpoint.url,
            headers=headers,
            body=delivery.payload_json,
            http_status=http_status,
            response_body=response_text,
            delivery_id=delivery.delivery_id,
            outcome=outcome,
        )
        return outcome

    async def run_once(self, limit: int = 10) -> int:
        from enterprise.gateway.config import config

        if not config.callback_enabled:
            return 0
        deliveries = await claim_pending_callback_deliveries(self.db, limit=limit)
        for delivery in deliveries:
            try:
                outcome = await self.deliver_one(delivery)
                logger.info(
                    "callback delivery outcome=%s delivery_id=%s "
                    "terminal_status=%s attempts=%s",
                    outcome,
                    delivery.delivery_id,
                    delivery.terminal_status,
                    delivery.attempts + 1,
                )
            except Exception:
                logger.exception(
                    "callback delivery iteration failed delivery_id=%s",
                    delivery.delivery_id,
                )
        return len(deliveries)

    async def run_forever(self, interval_seconds: float = 2.0) -> None:
        try:
            while True:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Callback delivery worker iteration failed")
                await asyncio.sleep(interval_seconds)
        finally:
            await self.close()


async def emit_terminal_callback_safe(
    db: aiosqlite.Connection,
    doc: ExtDocumentMap,
    terminal_status: TerminalStatus,
    *,
    quality_status: str | None = None,
    retrievable: bool = False,
    error: dict[str, Any] | None = None,
) -> None:
    """Enqueue without affecting the ingestion state machine."""
    await enqueue_terminal_callback(
        db,
        doc=doc,
        terminal_status=terminal_status,
        quality_status=quality_status,
        retrievable=retrievable,
        error=error,
    )
