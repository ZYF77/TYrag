"""Internal callback envelope, authentication, idempotency, and retry policy.

This module is deliberately transport-neutral. A production outbox or HTTP
delivery worker can use it after the external callback contract is approved.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


CallbackStatus = Literal["accepted", "replay", "conflict"]
DeliveryStatus = Literal["delivered", "retry_wait", "dead_letter"]


class CallbackSignatureError(ValueError):
    """The callback signature or timestamp is invalid."""


class CallbackPayloadConflict(ValueError):
    """An event ID was reused with a different payload."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _timestamped_payload(timestamp: int, payload: bytes) -> bytes:
    return str(timestamp).encode("ascii") + b"." + payload


@dataclass(frozen=True)
class CallbackEnvelope:
    event_id: str
    event_type: str
    tenant_id: str
    payload: dict[str, Any]
    occurred_at: str = ""
    schema_version: str = "m3.callback.v1"

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("event_type", self.event_type),
            ("tenant_id", self.tenant_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")

    @property
    def body(self) -> bytes:
        return _canonical_json(
            {
                "schemaVersion": self.schema_version,
                "eventId": self.event_id,
                "eventType": self.event_type,
                "tenantId": self.tenant_id,
                "occurredAt": self.occurred_at or datetime.now(timezone.utc).isoformat(),
                "payload": self.payload,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def sign_payload(payload: bytes, secret: str, timestamp: int) -> str:
    if not secret:
        raise ValueError("callback signing secret is required")
    digest = hmac.new(
        secret.encode("utf-8"),
        _timestamped_payload(timestamp, payload),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    payload: bytes,
    signature: str,
    secret: str,
    timestamp: int,
    *,
    now: int | None = None,
    max_age_seconds: int = 300,
) -> None:
    if not secret or not signature:
        raise CallbackSignatureError("callback signature is missing")
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_age_seconds:
        raise CallbackSignatureError("callback signature is outside the timestamp window")
    expected = sign_payload(payload, secret, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise CallbackSignatureError("callback signature is invalid")


@dataclass(frozen=True)
class IdempotencyResult:
    status: CallbackStatus
    payload_hash: str


class CallbackIdempotencyLedger:
    """Small in-memory policy object for tests and transport adapters.

    A persistent implementation must store the same event ID and payload hash
    atomically in the shared outbox/replay store.
    """

    def __init__(self) -> None:
        self._payload_hashes: dict[str, str] = {}

    def reserve(self, event_id: str, payload: bytes) -> IdempotencyResult:
        if not event_id:
            raise ValueError("event_id is required")
        payload_hash = hashlib.sha256(payload).hexdigest()
        previous = self._payload_hashes.get(event_id)
        if previous is None:
            self._payload_hashes[event_id] = payload_hash
            return IdempotencyResult("accepted", payload_hash)
        if hmac.compare_digest(previous, payload_hash):
            return IdempotencyResult("replay", payload_hash)
        raise CallbackPayloadConflict("event_id was reused with a different payload")


@dataclass(frozen=True)
class DeliveryDecision:
    status: DeliveryStatus
    retryable: bool
    attempt: int
    delay_seconds: float
    reason: str


def classify_delivery(
    http_status: int,
    *,
    attempt: int,
    max_attempts: int = 5,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 300.0,
) -> DeliveryDecision:
    if attempt < 1:
        raise ValueError("attempt must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if 200 <= http_status < 300:
        return DeliveryDecision("delivered", False, attempt, 0.0, "accepted")

    retryable = http_status == 408 or http_status == 429 or http_status >= 500
    if not retryable:
        return DeliveryDecision("dead_letter", False, attempt, 0.0, "permanent_http_error")
    if attempt >= max_attempts:
        return DeliveryDecision("dead_letter", True, attempt, 0.0, "retry_limit_exhausted")

    delay = min(
        max_delay_seconds,
        max(0.0, base_delay_seconds) * (2 ** (attempt - 1)),
    )
    return DeliveryDecision("retry_wait", True, attempt, delay, "transient_http_error")
