# Change Request: M3-B Callback Delivery Core

## Scope

This patch adds a transport-neutral callback envelope, HMAC signing policy,
event-id idempotency policy, and HTTP delivery classification. It is not wired
to a production callback URL or the existing document outbox.

## Contract Decisions Needed

- Subscriber registration and tenant-level authorization.
- Final callback URL, authentication, and key rotation mechanism.
- Event retention and replay endpoint behavior.
- Whether HTTP 408 and 429 are retryable for every subscriber.
- Dead-letter storage, operator controls, and delivery observability.

## Safety

- Event payloads are independent from citation evidence.
- Signature comparison is constant-time and timestamp bounded.
- Reusing an event ID with a different payload is a conflict.
- The module does not log secrets or document content.
