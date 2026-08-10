from __future__ import annotations

import pytest

from enterprise.gateway.callback import (
    CallbackEnvelope,
    CallbackIdempotencyLedger,
    CallbackPayloadConflict,
    CallbackSignatureError,
    classify_delivery,
    sign_payload,
    verify_signature,
)


def test_envelope_is_stable_and_keeps_business_payload_separate():
    envelope = CallbackEnvelope(
        event_id="evt-1",
        event_type="message.completed",
        tenant_id="tenant-a",
        occurred_at="2026-01-01T00:00:00+00:00",
        payload={"status": "completed", "citations": []},
    )

    assert envelope.as_dict() == {
        "eventId": "evt-1",
        "eventType": "message.completed",
        "occurredAt": "2026-01-01T00:00:00+00:00",
        "payload": {"citations": [], "status": "completed"},
        "schemaVersion": "m3.callback.v1",
        "tenantId": "tenant-a",
    }


def test_signature_accepts_current_payload_and_rejects_replay_or_tampering():
    payload = b'{"eventId":"evt-1"}'
    signature = sign_payload(payload, "callback-secret", 100)

    verify_signature(payload, signature, "callback-secret", 100, now=120)
    with pytest.raises(CallbackSignatureError):
        verify_signature(payload, signature, "callback-secret", 100, now=500)
    with pytest.raises(CallbackSignatureError):
        verify_signature(b'{"eventId":"evt-2"}', signature, "callback-secret", 100, now=120)


def test_idempotency_replays_same_payload_and_conflicts_on_different_payload():
    ledger = CallbackIdempotencyLedger()

    first = ledger.reserve("evt-1", b"one")
    replay = ledger.reserve("evt-1", b"one")

    assert first.status == "accepted"
    assert replay.status == "replay"
    with pytest.raises(CallbackPayloadConflict):
        ledger.reserve("evt-1", b"two")


@pytest.mark.parametrize("status", [200, 201, 204])
def test_successful_http_responses_are_terminal(status):
    decision = classify_delivery(status, attempt=1)
    assert decision.status == "delivered"
    assert decision.retryable is False


def test_transient_http_responses_backoff_and_eventually_dead_letter():
    retry = classify_delivery(503, attempt=2, max_attempts=5, base_delay_seconds=2)
    exhausted = classify_delivery(503, attempt=5, max_attempts=5)

    assert retry.status == "retry_wait"
    assert retry.retryable is True
    assert retry.delay_seconds == 4
    assert exhausted.status == "dead_letter"
    assert exhausted.reason == "retry_limit_exhausted"


def test_client_errors_are_not_retried_except_rate_limit_and_timeout():
    permanent = classify_delivery(422, attempt=1)
    timeout = classify_delivery(408, attempt=1)
    rate_limited = classify_delivery(429, attempt=1)

    assert permanent.status == "dead_letter"
    assert permanent.retryable is False
    assert timeout.status == "retry_wait"
    assert rate_limited.status == "retry_wait"
