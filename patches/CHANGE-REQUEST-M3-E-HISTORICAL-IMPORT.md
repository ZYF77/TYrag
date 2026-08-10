# CHANGE-REQUEST: Internal Historical Parsing Import

Status: Open

## Reason

The frozen v2 document OpenAPI does not define historical batch import,
checkpoint recovery, or human review operations. The M3-E slice therefore
keeps these operations behind an Enterprise-internal adapter and does not add
routes or schemas to the main OpenAPI baseline.

## Minimal Change

- Add `enterprise.gateway.parsing.historical_import.HistoricalImportService`.
- Persist batch, item, checkpoint, review, and append-only audit records in
  Enterprise-owned tables with the `parsing_` prefix.
- Delegate each item to the existing `SyncService.process_event` callback.
- Reuse the existing document state machine and existing `review_required`,
  `failed`, and `retry_wait` states. No new global document status is added.
- Persist parser application state and quality status snapshots for replay;
  citations are never used to derive business state.

## Contract and Compatibility

- No modification to `ragflow/**`, official migrations, root lock files,
  frozen OpenAPI, or upstream/global status enums.
- `DOCUMENT_VERSION_CONFLICT`, event conflicts, and duplicate-file outcomes
  are reported inside the adapter item outcome, not added to the public error
  contract.
- M2 remains blocked: local unit/adapter contract tests are not live
  RAGFlow/object-storage integration evidence.

## Risks and Rollback

- The adapter currently uses the existing Enterprise SQLite connection; a
  production multi-instance deployment needs a reviewed repository/locking
  design before enabling parallel workers.
- Cross-system document processing is not a distributed transaction. The
  persisted item event id and existing SyncService idempotency make a replay
  safe, but reconciliation remains required after an abrupt process loss.
- Rollback is deleting the new adapter package and its `parsing_` tables; the
  existing document mappings and frozen APIs are unaffected.
