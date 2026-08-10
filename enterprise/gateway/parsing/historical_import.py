"""Internal batch adapter for historical parsing and human review.

This module stays behind the Enterprise adapter boundary. It persists batch,
checkpoint, review, and audit evidence, then delegates document work to the
existing ``SyncService.process_event`` callback. It does not add a public API,
change the document status enum, or infer business state from citations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

import aiosqlite

from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.quality.models import get_latest_evaluation
from enterprise.gateway.sync.models import (
    OutboxEvent,
    get_mapping,
    get_mapping_by_event_id,
    get_outbox_by_event_id,
    row_to_mapping,
)
from enterprise.gateway.sync.state_machine import (
    is_terminal_document_status,
    transition_allowed,
    validate_transition,
)
from enterprise.gateway.sync.sync_service import DocumentSyncError


# These are adapter-local states. Document and quality state remains owned by
# the existing sync/quality contracts below this package.
BATCH_STATES = frozenset({
    "pending", "running", "review_required", "failed", "completed",
})
ITEM_STATES = frozenset({
    "pending", "processing", "completed", "deduplicated", "conflict",
    "failed", "review_required", "rejected",
})
REVIEW_STATES = frozenset({
    "review_required", "approved", "rejected", "retry_queued",
})
TERMINAL_ITEM_STATES = frozenset({
    "completed", "deduplicated", "conflict", "rejected",
})
RETRYABLE_ITEM_STATES = frozenset({"failed", "rejected", "review_required"})
REVIEW_DECISIONS = frozenset({"approve", "reject", "retry"})

_MAX_REASON_LENGTH = 1000
_MAX_ERROR_LENGTH = 1000


CREATE_HISTORICAL_IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS parsing_import_batch (
    batch_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_items INTEGER NOT NULL,
    checkpoint_sequence INTEGER NOT NULL DEFAULT -1,
    checkpoint_item_id INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    completed_items INTEGER NOT NULL DEFAULT 0,
    deduplicated_items INTEGER NOT NULL DEFAULT 0,
    conflict_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    review_required_items INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, source_system, idempotency_key)
);

CREATE TABLE IF NOT EXISTS parsing_import_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    effective_event_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    item_status TEXT NOT NULL DEFAULT 'pending',
    outcome_code TEXT,
    retryable INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    claimed_at TEXT,
    worker_id TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    document_sync_status TEXT,
    document_business_status TEXT,
    document_current_version INTEGER,
    parser_application_status TEXT,
    quality_status TEXT,
    persisted_state_json TEXT NOT NULL DEFAULT '{}',
    duplicate_of_item_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, sequence),
    FOREIGN KEY(batch_id) REFERENCES parsing_import_batch(batch_id)
);

CREATE INDEX IF NOT EXISTS idx_parsing_import_item_status
    ON parsing_import_item(batch_id, item_status, sequence);
CREATE INDEX IF NOT EXISTS idx_parsing_import_item_identity
    ON parsing_import_item(tenant_id, source_system,
                           external_document_id, source_version_id);

CREATE TABLE IF NOT EXISTS parsing_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'review_required',
    decision TEXT,
    operator_id TEXT,
    reviewed_at TEXT,
    reason TEXT NOT NULL,
    before_state_json TEXT NOT NULL DEFAULT '{}',
    after_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES parsing_import_batch(batch_id),
    FOREIGN KEY(item_id) REFERENCES parsing_import_item(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_parsing_review_open_item
    ON parsing_review(item_id) WHERE review_status='review_required';
CREATE INDEX IF NOT EXISTS idx_parsing_review_queue
    ON parsing_review(tenant_id, review_status, created_at);

CREATE TABLE IF NOT EXISTS parsing_audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    batch_id TEXT,
    item_id INTEGER,
    review_id INTEGER,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    before_state_json TEXT NOT NULL DEFAULT '{}',
    after_state_json TEXT NOT NULL DEFAULT '{}',
    request_id TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parsing_audit_tenant_time
    ON parsing_audit_event(tenant_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_parsing_audit_batch
    ON parsing_audit_event(batch_id, occurred_at, id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _bounded(value: Any, limit: int = _MAX_ERROR_LENGTH) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _mapping_value(mapping: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _canonical_payload(payload: dict[str, Any]) -> str:
    stable = dict(payload)
    for key in ("eventId", "event_id", "batchId", "batch_id"):
        stable.pop(key, None)
    return _json_dumps(stable)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_json(state: dict[str, Any]) -> str:
    return _json_dumps(state)


@dataclass(frozen=True)
class HistoricalImportItem:
    """Internal, source-neutral historical file descriptor.

    The descriptor contains coordinates and metadata only. File bytes remain
    behind the existing source adapter and are never copied into this queue.
    """

    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    sha256: str
    file_name: str
    source: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    media_type: str = "application/pdf"
    event_id: str | None = None
    event_type: str = "upsert"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "HistoricalImportItem":
        if not isinstance(value, dict):
            raise ValueError("historical import item must be an object")
        tenant_id = _mapping_value(value, "tenant_id", "tenantId")
        source_system = _mapping_value(value, "source_system", "sourceSystem")
        external_document_id = _mapping_value(
            value, "external_document_id", "externalDocumentId",
        )
        source_version_id = _mapping_value(
            value, "source_version_id", "sourceVersionId",
        )
        sha256 = str(_mapping_value(value, "sha256", default="")).lower()
        file_name = _mapping_value(value, "file_name", "fileName")
        source = _mapping_value(value, "source", default={})
        metadata = _mapping_value(value, "metadata", default={})
        media_type = _mapping_value(
            value, "media_type", "mediaType", default="application/pdf",
        )
        event_id = _mapping_value(value, "event_id", "eventId")
        event_type = _mapping_value(
            value, "event_type", "eventType", default="upsert",
        )

        required = {
            "tenant_id": tenant_id,
            "source_system": source_system,
            "external_document_id": external_document_id,
            "source_version_id": source_version_id,
            "file_name": file_name,
        }
        if any(
            not isinstance(item, str) or not item.strip()
            for item in required.values()
        ):
            raise ValueError("historical import identity and file_name are required")
        if len(sha256) != 64 or any(
            char not in "0123456789abcdef" for char in sha256
        ):
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest")
        if not isinstance(source, dict) or not isinstance(metadata, dict):
            raise ValueError("source and metadata must be objects")
        if event_type not in ("upsert", "reindex"):
            raise ValueError("event_type must be upsert or reindex")

        normalized_metadata = dict(metadata)
        normalized_metadata.setdefault("tenant_id", tenant_id)
        normalized_metadata.setdefault("source_system", source_system)
        normalized_metadata.setdefault("external_document_id", external_document_id)
        normalized_metadata.setdefault("document_version", source_version_id)
        return cls(
            tenant_id=tenant_id.strip(),
            source_system=source_system.strip(),
            external_document_id=external_document_id.strip(),
            source_version_id=source_version_id.strip(),
            sha256=sha256,
            file_name=file_name.strip(),
            source=dict(source),
            metadata=normalized_metadata,
            media_type=str(media_type or "application/pdf"),
            event_id=str(event_id) if event_id else None,
            event_type=event_type,
        )

    def payload(self, batch_id: str, event_id: str) -> dict[str, Any]:
        return {
            "eventId": event_id,
            "eventType": self.event_type,
            "tenantId": self.tenant_id,
            "sourceSystem": self.source_system,
            "externalDocumentId": self.external_document_id,
            "sourceVersionId": self.source_version_id,
            "sha256": self.sha256,
            "fileName": self.file_name,
            "mediaType": self.media_type,
            "source": dict(self.source),
            "metadata": dict(self.metadata),
            "batchId": batch_id,
        }

    def canonical_payload(self) -> dict[str, Any]:
        return self.payload("", self.event_id or "")

    def to_event(
        self, batch_id: str, event_id: str | None = None,
    ) -> OutboxEvent:
        effective_event_id = event_id or self.event_id
        if not effective_event_id:
            raise ValueError("event_id is required to build an outbox event")
        payload = self.payload(batch_id, effective_event_id)
        return OutboxEvent(
            event_id=effective_event_id,
            event_type=self.event_type,
            tenant_id=self.tenant_id,
            source_system=self.source_system,
            external_document_id=self.external_document_id,
            source_version_id=self.source_version_id,
            payload=_json_dumps(payload),
            batch_id=batch_id,
        )


@dataclass
class BatchRecord:
    batch_id: str
    tenant_id: str
    source_system: str
    idempotency_key: str
    manifest_hash: str
    status: str
    total_items: int
    checkpoint_sequence: int
    checkpoint_item_id: int | None
    attempt_count: int
    completed_items: int
    deduplicated_items: int
    conflict_items: int
    failed_items: int
    review_required_items: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "batchId": self.batch_id,
            "tenantId": self.tenant_id,
            "sourceSystem": self.source_system,
            "idempotencyKey": self.idempotency_key,
            "manifestHash": self.manifest_hash,
            "status": self.status,
            "totalItems": self.total_items,
            "checkpoint": {
                "sequence": self.checkpoint_sequence,
                "itemId": self.checkpoint_item_id,
            },
            "attemptCount": self.attempt_count,
            "completedItems": self.completed_items,
            "deduplicatedItems": self.deduplicated_items,
            "conflictItems": self.conflict_items,
            "failedItems": self.failed_items,
            "reviewRequiredItems": self.review_required_items,
            "lastErrorCode": self.last_error_code,
            "lastErrorMessage": self.last_error_message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class ImportItemRecord:
    id: int
    batch_id: str
    sequence: int
    item_key: str
    event_id: str
    effective_event_id: str
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    sha256: str
    payload_hash: str
    item_status: str
    outcome_code: str | None
    retryable: bool
    attempt_count: int
    next_retry_at: str | None
    claimed_at: str | None
    worker_id: str | None
    last_error_code: str | None
    last_error_message: str | None
    document_sync_status: str | None
    document_business_status: str | None
    document_current_version: bool | None
    parser_application_status: str | None
    quality_status: str | None
    persisted_state: dict[str, Any]
    duplicate_of_item_id: int | None
    created_at: str
    updated_at: str
    payload_json: str = ""

    @property
    def persisted_business_state(self) -> dict[str, Any]:
        """Return stored state; never derive it from citations."""
        return dict(self.persisted_state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "itemId": self.id,
            "batchId": self.batch_id,
            "sequence": self.sequence,
            "itemKey": self.item_key,
            "eventId": self.event_id,
            "status": self.item_status,
            "outcomeCode": self.outcome_code,
            "retryable": self.retryable,
            "attemptCount": self.attempt_count,
            "nextRetryAt": self.next_retry_at,
            "lastErrorCode": self.last_error_code,
            "lastErrorMessage": self.last_error_message,
            "documentState": self.persisted_business_state,
            "duplicateOfItemId": self.duplicate_of_item_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class ReviewRecord:
    id: int
    batch_id: str
    item_id: int
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    review_status: str
    decision: str | None
    operator_id: str | None
    reviewed_at: str | None
    reason: str
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewId": self.id,
            "batchId": self.batch_id,
            "itemId": self.item_id,
            "tenantId": self.tenant_id,
            "sourceSystem": self.source_system,
            "externalDocumentId": self.external_document_id,
            "sourceVersionId": self.source_version_id,
            "status": self.review_status,
            "decision": self.decision,
            "operatorId": self.operator_id,
            "reviewedAt": self.reviewed_at,
            "reason": self.reason,
            "beforeState": dict(self.before_state),
            "afterState": dict(self.after_state),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class AuditRecord:
    id: int
    tenant_id: str
    batch_id: str | None
    item_id: int | None
    review_id: int | None
    actor_id: str
    action: str
    reason: str | None
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    request_id: str | None
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "auditId": self.id,
            "tenantId": self.tenant_id,
            "batchId": self.batch_id,
            "itemId": self.item_id,
            "reviewId": self.review_id,
            "actorId": self.actor_id,
            "action": self.action,
            "reason": self.reason,
            "beforeState": dict(self.before_state),
            "afterState": dict(self.after_state),
            "requestId": self.request_id,
            "occurredAt": self.occurred_at,
        }


class HistoricalImportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BatchConflictError(HistoricalImportError):
    def __init__(self, message: str = "Batch idempotency key conflicts with another manifest"):
        super().__init__("BATCH_IDEMPOTENCY_CONFLICT", message)


class ImportPermissionError(HistoricalImportError):
    def __init__(self, message: str = "Access denied"):
        super().__init__("ACL_DENIED", message)


class ReviewStateError(HistoricalImportError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


Processor = Callable[[OutboxEvent], Awaitable[Any]]


def _row_to_batch(row: aiosqlite.Row) -> BatchRecord:
    return BatchRecord(
        batch_id=row["batch_id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        idempotency_key=row["idempotency_key"],
        manifest_hash=row["manifest_hash"],
        status=row["status"],
        total_items=row["total_items"],
        checkpoint_sequence=row["checkpoint_sequence"],
        checkpoint_item_id=row["checkpoint_item_id"],
        attempt_count=row["attempt_count"],
        completed_items=row["completed_items"],
        deduplicated_items=row["deduplicated_items"],
        conflict_items=row["conflict_items"],
        failed_items=row["failed_items"],
        review_required_items=row["review_required_items"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_item(row: aiosqlite.Row) -> ImportItemRecord:
    return ImportItemRecord(
        id=row["id"],
        batch_id=row["batch_id"],
        sequence=row["sequence"],
        item_key=row["item_key"],
        event_id=row["event_id"],
        effective_event_id=row["effective_event_id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        sha256=row["sha256"],
        payload_hash=row["payload_hash"],
        item_status=row["item_status"],
        outcome_code=row["outcome_code"],
        retryable=bool(row["retryable"]),
        attempt_count=row["attempt_count"],
        next_retry_at=row["next_retry_at"],
        claimed_at=row["claimed_at"],
        worker_id=row["worker_id"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        document_sync_status=row["document_sync_status"],
        document_business_status=row["document_business_status"],
        document_current_version=(
            None
            if row["document_current_version"] is None
            else bool(row["document_current_version"])
        ),
        parser_application_status=row["parser_application_status"],
        quality_status=row["quality_status"],
        persisted_state=_json_loads(row["persisted_state_json"], {}),
        duplicate_of_item_id=row["duplicate_of_item_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload_json=row["payload_json"],
    )


def _row_to_review(row: aiosqlite.Row) -> ReviewRecord:
    return ReviewRecord(
        id=row["id"],
        batch_id=row["batch_id"],
        item_id=row["item_id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        review_status=row["review_status"],
        decision=row["decision"],
        operator_id=row["operator_id"],
        reviewed_at=row["reviewed_at"],
        reason=row["reason"],
        before_state=_json_loads(row["before_state_json"], {}),
        after_state=_json_loads(row["after_state_json"], {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_audit(row: aiosqlite.Row) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        batch_id=row["batch_id"],
        item_id=row["item_id"],
        review_id=row["review_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        reason=row["reason"],
        before_state=_json_loads(row["before_state_json"], {}),
        after_state=_json_loads(row["after_state_json"], {}),
        request_id=row["request_id"],
        occurred_at=row["occurred_at"],
    )


class HistoricalImportService:
    """Checkpointed batch adapter with explicit review and audit boundaries."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        processor: Processor | None = None,
        *,
        sync_service: Any | None = None,
        stale_after_seconds: int = 900,
    ) -> None:
        self.db = db
        self.processor = processor or getattr(sync_service, "process_event", None)
        self.stale_after_seconds = stale_after_seconds

    async def ensure_schema(self) -> None:
        await self.db.executescript(CREATE_HISTORICAL_IMPORT_SCHEMA)
        await self.db.commit()

    @staticmethod
    def _actor_id(principal: Any | None, fallback: str | None = None) -> str:
        if isinstance(principal, UserPrincipal):
            return principal.business_user_id
        if isinstance(principal, ServicePrincipal):
            return principal.credential_id or principal.source_system
        return fallback or "internal"

    @staticmethod
    def _authorize(
        principal: Any | None,
        tenant_id: str,
        source_system: str,
        *,
        capability: str | None = None,
        internal_allowed: bool = True,
    ) -> None:
        if principal is None:
            if internal_allowed:
                return
            raise ImportPermissionError()
        if isinstance(principal, UserPrincipal):
            if not principal.is_active or principal.tenant_id != tenant_id:
                raise ImportPermissionError()
            if (
                capability
                and capability not in principal.capabilities
                and "admin" not in principal.capabilities
            ):
                raise ImportPermissionError()
            return
        if isinstance(principal, ServicePrincipal):
            if not principal.authenticated:
                raise ImportPermissionError()
            if (
                principal.allowed_bindings
                and (tenant_id, source_system) not in principal.allowed_bindings
            ):
                raise ImportPermissionError()
            if (
                principal.source_system not in {"service", "anonymous", source_system}
                and not principal.allowed_bindings
            ):
                raise ImportPermissionError()
            return
        raise ImportPermissionError()

    async def submit_batch(
        self,
        batch_id: str,
        items: Iterable[HistoricalImportItem | dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        tenant_id: str | None = None,
        source_system: str | None = None,
        principal: Any | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> BatchRecord:
        """Create or replay one immutable batch manifest.

        Reusing the same batch/idempotency key with the same manifest is a
        read-only replay. A changed manifest is a conflict and cannot append
        to an existing batch.
        """
        await self.ensure_schema()
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise HistoricalImportError("VALIDATION_ERROR", "batch_id is required")
        normalized = [
            item
            if isinstance(item, HistoricalImportItem)
            else HistoricalImportItem.from_mapping(item)
            for item in items
        ]
        if not normalized:
            raise HistoricalImportError("VALIDATION_ERROR", "batch must contain items")
        first_tenant = tenant_id or normalized[0].tenant_id
        first_source = source_system or normalized[0].source_system
        if any(
            item.tenant_id != first_tenant or item.source_system != first_source
            for item in normalized
        ):
            raise HistoricalImportError(
                "VALIDATION_ERROR",
                "a batch cannot mix tenant or source_system scopes",
            )
        self._authorize(
            principal, first_tenant, first_source, capability="upload",
        )
        idempotency_key = idempotency_key or batch_id
        manifest_hash = _sha256(
            _json_dumps([item.canonical_payload() for item in normalized])
        )
        existing = await self._get_batch_by_id(batch_id)
        if existing:
            if (
                existing.tenant_id == first_tenant
                and existing.source_system == first_source
                and existing.manifest_hash == manifest_hash
                and existing.idempotency_key == idempotency_key
            ):
                return existing
            raise BatchConflictError("batch_id was already used by another manifest")
        existing_key = await self._get_batch_by_idempotency(
            first_tenant, first_source, idempotency_key,
        )
        if existing_key:
            if existing_key.manifest_hash == manifest_hash:
                return existing_key
            raise BatchConflictError()

        now = _utc_now()
        actor = self._actor_id(principal, actor_id)
        request_id = request_id or str(uuid.uuid4())
        try:
            await self.db.execute("BEGIN IMMEDIATE")
            await self.db.execute(
                """INSERT INTO parsing_import_batch
                   (batch_id, tenant_id, source_system, idempotency_key,
                    manifest_hash, status, total_items, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    batch_id,
                    first_tenant,
                    first_source,
                    idempotency_key,
                    manifest_hash,
                    len(normalized),
                    now,
                    now,
                ),
            )
            seen_identities: dict[str, tuple[str, int]] = {}
            seen_files: dict[str, tuple[str, int]] = {}
            seen_events: dict[str, tuple[str, int]] = {}
            for sequence, item in enumerate(normalized):
                event_id = item.event_id or f"{batch_id}:{sequence}"
                payload = item.payload(batch_id, event_id)
                payload_json = _json_dumps(payload)
                payload_hash = _sha256(_canonical_payload(payload))
                identity_key = self._identity_key(item)
                status, outcome, effective_event_id, duplicate_of = (
                    await self._initial_item_decision(
                        item,
                        identity_key,
                        event_id,
                        sequence,
                        seen_identities,
                        seen_files,
                        seen_events,
                    )
                )
                state = {
                    "sync_status": None,
                    "business_status": None,
                    "current_version": False,
                    "parser_application_status": None,
                    "quality_status": None,
                    "source_version_id": item.source_version_id,
                }
                cursor = await self.db.execute(
                    """INSERT INTO parsing_import_item
                       (batch_id, sequence, item_key, event_id,
                        effective_event_id, tenant_id, source_system,
                        external_document_id, source_version_id, sha256,
                        payload_hash, payload_json, item_status, outcome_code,
                        persisted_state_json, duplicate_of_item_id,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        sequence,
                        identity_key,
                        event_id,
                        effective_event_id,
                        item.tenant_id,
                        item.source_system,
                        item.external_document_id,
                        item.source_version_id,
                        item.sha256,
                        payload_hash,
                        payload_json,
                        status,
                        outcome,
                        _state_json(state),
                        duplicate_of,
                        now,
                        now,
                    ),
                )
                await self._insert_audit(
                    tenant_id=item.tenant_id,
                    batch_id=batch_id,
                    item_id=cursor.lastrowid,
                    actor_id=actor,
                    action="batch_item_received",
                    reason=outcome,
                    before_state={},
                    after_state={"item_status": status, "outcome_code": outcome},
                    request_id=request_id,
                    commit=False,
                )
            await self._refresh_batch(batch_id, commit=False)
            await self._insert_audit(
                tenant_id=first_tenant,
                batch_id=batch_id,
                item_id=None,
                actor_id=actor,
                action="batch_received",
                reason="historical_import_submitted",
                before_state={},
                after_state={"status": "pending", "total_items": len(normalized)},
                request_id=request_id,
                commit=False,
            )
            await self.db.commit()
        except sqlite3.IntegrityError as exc:
            await self.db.rollback()
            raise BatchConflictError("batch was created concurrently") from exc
        return await self.get_batch(batch_id)

    async def _initial_item_decision(
        self,
        item: HistoricalImportItem,
        identity_key: str,
        event_id: str,
        sequence: int,
        seen_identities: dict[str, tuple[str, int]],
        seen_files: dict[str, tuple[str, int]],
        seen_events: dict[str, tuple[str, int]],
    ) -> tuple[str, str | None, str, int | None]:
        event_hash = _sha256(_canonical_payload(item.payload("", event_id)))
        if event_id in seen_events:
            previous_hash, previous_sequence = seen_events[event_id]
            if previous_hash != event_hash:
                return "conflict", "EVENT_ID_CONFLICT", event_id, None
            return "deduplicated", "DUPLICATE_EVENT", event_id, previous_sequence
        seen_events[event_id] = (event_hash, sequence)

        previous_identity = seen_identities.get(identity_key)
        if previous_identity:
            previous_sha, previous_sequence = previous_identity
            if previous_sha != item.sha256:
                return "conflict", "DOCUMENT_VERSION_CONFLICT", event_id, None
            return "deduplicated", "DUPLICATE_VERSION", event_id, previous_sequence
        seen_identities[identity_key] = (item.sha256, sequence)

        previous_file = seen_files.get(item.sha256)
        if previous_file and previous_file[0] != identity_key:
            return "conflict", "DOCUMENT_DUPLICATE_FILE", event_id, previous_file[1]
        seen_files[item.sha256] = (identity_key, sequence)

        existing = await get_mapping(
            self.db,
            item.tenant_id,
            item.source_system,
            item.external_document_id,
            item.source_version_id,
        )
        if existing:
            if existing.sha256.lower() != item.sha256:
                return "conflict", "DOCUMENT_VERSION_CONFLICT", event_id, None
            if (
                is_terminal_document_status(existing.sync_status)
                and existing.event_status in {"completed", "tracking"}
            ):
                return "deduplicated", "DUPLICATE_VERSION", event_id, None
            return "pending", "RETRY_EXISTING_VERSION", existing.event_id, None

        async with self.db.execute(
            """SELECT * FROM ext_document_map
               WHERE tenant_id=? AND sha256=?
               ORDER BY updated_at DESC LIMIT 1""",
            (item.tenant_id, item.sha256),
        ) as cursor:
            duplicate_row = await cursor.fetchone()
        if duplicate_row:
            duplicate = row_to_mapping(duplicate_row)
            if (
                duplicate.external_document_id != item.external_document_id
                or duplicate.source_version_id != item.source_version_id
            ):
                return "conflict", "DOCUMENT_DUPLICATE_FILE", event_id, None
        # An existing outbox/event with the same id but no mapping is also a
        # conflict. Reusing it would make the batch unable to prove its scope.
        if await get_mapping_by_event_id(self.db, event_id) is not None:
            return "conflict", "EVENT_ID_CONFLICT", event_id, None
        if await get_outbox_by_event_id(self.db, event_id) is not None:
            return "conflict", "EVENT_ID_CONFLICT", event_id, None
        return "pending", None, event_id, None

    @staticmethod
    def _identity_key(item: HistoricalImportItem) -> str:
        return "|".join((
            item.tenant_id,
            item.source_system,
            item.external_document_id,
            item.source_version_id,
        ))

    async def _get_batch_by_id(self, batch_id: str) -> BatchRecord | None:
        async with self.db.execute(
            "SELECT * FROM parsing_import_batch WHERE batch_id=?",
            (batch_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_batch(row) if row else None

    async def _get_batch_by_idempotency(
        self,
        tenant_id: str,
        source_system: str,
        idempotency_key: str,
    ) -> BatchRecord | None:
        async with self.db.execute(
            """SELECT * FROM parsing_import_batch
               WHERE tenant_id=? AND source_system=? AND idempotency_key=?""",
            (tenant_id, source_system, idempotency_key),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_batch(row) if row else None

    async def _get_item(self, item_id: int) -> ImportItemRecord | None:
        async with self.db.execute(
            "SELECT * FROM parsing_import_item WHERE id=?",
            (item_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_item(row) if row else None

    async def _get_review(self, review_id: int) -> ReviewRecord | None:
        async with self.db.execute(
            "SELECT * FROM parsing_review WHERE id=?",
            (review_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_review(row) if row else None

    async def _get_open_review(self, item_id: int) -> ReviewRecord | None:
        async with self.db.execute(
            """SELECT * FROM parsing_review
               WHERE item_id=? AND review_status='review_required'""",
            (item_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_review(row) if row else None

    async def get_batch(self, batch_id: str) -> BatchRecord:
        await self.ensure_schema()
        batch = await self._get_batch_by_id(batch_id)
        if not batch:
            raise HistoricalImportError("BATCH_NOT_FOUND", "Batch not found")
        return batch

    async def list_items(
        self,
        batch_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ImportItemRecord]:
        await self.ensure_schema()
        clauses = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if status:
            if status not in ITEM_STATES:
                raise HistoricalImportError("VALIDATION_ERROR", "unknown item status")
            clauses.append("item_status=?")
            params.append(status)
        params.append(max(1, min(limit, 500)))
        async with self.db.execute(
            f"""SELECT * FROM parsing_import_item
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence LIMIT ?""",
            params,
        ) as cursor:
            return [_row_to_item(row) for row in await cursor.fetchall()]

    async def run_batch(
        self,
        batch_id: str,
        *,
        limit: int | None = None,
        worker_id: str | None = None,
        recover_stale: bool = True,
    ) -> BatchRecord:
        await self.ensure_schema()
        if self.processor is None:
            raise HistoricalImportError(
                "PROCESSOR_NOT_CONFIGURED",
                "historical import requires a SyncService.process_event adapter",
            )
        worker_id = worker_id or f"historical-{uuid.uuid4().hex[:10]}"
        if recover_stale:
            await self.recover_checkpoint(batch_id)
        processed = 0
        while limit is None or processed < limit:
            item = await self._claim_next_item(batch_id, worker_id)
            if item is None:
                break
            await self._process_item(item, worker_id)
            processed += 1
        await self._refresh_batch(batch_id)
        return await self.get_batch(batch_id)

    async def _claim_next_item(
        self, batch_id: str, worker_id: str,
    ) -> ImportItemRecord | None:
        now = _utc_now()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                """SELECT * FROM parsing_import_item
                   WHERE batch_id=? AND item_status='pending'
                   ORDER BY sequence LIMIT 1""",
                (batch_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await self.db.rollback()
                return None
            await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status='processing', attempt_count=attempt_count+1,
                       claimed_at=?, worker_id=?, next_retry_at=NULL,
                       updated_at=? WHERE id=? AND item_status='pending'""",
                (now, worker_id, now, row["id"]),
            )
            await self.db.execute(
                """UPDATE parsing_import_batch
                   SET status='running', attempt_count=attempt_count+1,
                       updated_at=? WHERE batch_id=?""",
                (now, batch_id),
            )
            await self._insert_audit(
                tenant_id=row["tenant_id"],
                batch_id=batch_id,
                item_id=row["id"],
                actor_id=worker_id,
                action="item_claimed",
                reason="checkpoint_item_claimed",
                before_state={"item_status": "pending"},
                after_state={"item_status": "processing", "worker_id": worker_id},
                request_id=None,
                commit=False,
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return await self._get_item(row["id"])

    async def recover_checkpoint(
        self,
        batch_id: str,
        *,
        stale_after_seconds: int | None = None,
        actor_id: str = "checkpoint-reconciler",
    ) -> int:
        await self.ensure_schema()
        stale_after_seconds = (
            self.stale_after_seconds
            if stale_after_seconds is None
            else max(0, stale_after_seconds)
        )
        threshold = (
            datetime.now(timezone.utc)
            - timedelta(seconds=stale_after_seconds)
        ).isoformat()
        async with self.db.execute(
            """SELECT * FROM parsing_import_item
               WHERE batch_id=? AND item_status='processing'
                 AND (claimed_at IS NULL OR claimed_at < ?)
               ORDER BY sequence""",
            (batch_id, threshold),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return 0
        now = _utc_now()
        recovered = 0
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                before = {
                    "item_status": row["item_status"],
                    "worker_id": row["worker_id"],
                }
                after = {
                    "item_status": "pending",
                    "last_error_code": "BATCH_WORKER_INTERRUPTED",
                }
                cursor = await self.db.execute(
                    """UPDATE parsing_import_item
                       SET item_status='pending', claimed_at=NULL, worker_id=NULL,
                           retryable=1, last_error_code=?,
                           last_error_message=?, updated_at=?
                       WHERE id=? AND item_status='processing'
                         AND (claimed_at IS NULL OR claimed_at < ?)""",
                    (
                        "BATCH_WORKER_INTERRUPTED",
                        "Processing lease expired before checkpoint commit",
                        now,
                        row["id"],
                        threshold,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                await self._insert_audit(
                    tenant_id=row["tenant_id"],
                    batch_id=batch_id,
                    item_id=row["id"],
                    actor_id=actor_id,
                    action="checkpoint_recovered",
                    reason="processing_lease_expired",
                    before_state=before,
                    after_state=after,
                    request_id=None,
                    commit=False,
                )
            await self._refresh_batch(batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return recovered

    async def _process_item(
        self, item: ImportItemRecord, worker_id: str,
    ) -> None:
        event_payload = _json_loads(item.payload_json, {})
        item_descriptor = HistoricalImportItem.from_mapping(event_payload)
        event = item_descriptor.to_event(
            item.batch_id,
            event_id=item.effective_event_id,
        )
        try:
            result = await self.processor(event)  # type: ignore[misc]
            doc = result[0] if isinstance(result, tuple) else result
            deduplicated = bool(result[1]) if isinstance(result, tuple) and len(result) > 1 else False
            if doc is None:
                doc = await get_mapping(
                    self.db,
                    item.tenant_id,
                    item.source_system,
                    item.external_document_id,
                    item.source_version_id,
                )
            evaluation = await get_latest_evaluation(
                self.db,
                item.tenant_id,
                item.source_system,
                item.external_document_id,
                item.source_version_id,
            )
            state = await self._document_state(doc, evaluation)
            needs_review = self._needs_review(doc, evaluation)
            await self._record_item_result(
                item,
                item_status="review_required" if needs_review else (
                    "deduplicated" if deduplicated else "completed"
                ),
                outcome_code=(
                    "REVIEW_REQUIRED"
                    if needs_review
                    else ("DUPLICATED" if deduplicated else "COMPLETED")
                ),
                retryable=False,
                state=state,
                error_code=None,
                error_message=None,
                worker_id=worker_id,
                review_reason=(
                    ";".join(self._review_reasons(doc, evaluation))
                    if needs_review
                    else None
                ),
            )
        except DocumentSyncError as exc:
            await self._record_processing_failure(item, exc, worker_id)
        except Exception:
            await self._record_processing_failure(
                item,
                DocumentSyncError("INTERNAL_ERROR", "Historical import processing failed", True),
                worker_id,
            )

    async def _record_processing_failure(
        self,
        item: ImportItemRecord,
        error: DocumentSyncError,
        worker_id: str,
    ) -> None:
        doc = await get_mapping(
            self.db,
            item.tenant_id,
            item.source_system,
            item.external_document_id,
            item.source_version_id,
        )
        evaluation = await get_latest_evaluation(
            self.db,
            item.tenant_id,
            item.source_system,
            item.external_document_id,
            item.source_version_id,
        )
        state = await self._document_state(doc, evaluation)
        needs_review = self._needs_review(doc, evaluation) or not error.retryable
        await self._record_item_result(
            item,
            item_status="review_required" if needs_review else "failed",
            outcome_code=error.code,
            retryable=not needs_review and error.retryable,
            state=state,
            error_code=error.code,
            error_message=str(error),
            worker_id=worker_id,
            review_reason=(
                ";".join(self._review_reasons(doc, evaluation))
                or str(error)
                if needs_review
                else None
            ),
        )

    async def _record_item_result(
        self,
        item: ImportItemRecord,
        *,
        item_status: str,
        outcome_code: str | None,
        retryable: bool,
        state: dict[str, Any],
        error_code: str | None,
        error_message: str | None,
        worker_id: str,
        review_reason: str | None,
    ) -> None:
        if item_status not in ITEM_STATES:
            raise HistoricalImportError("INTERNAL_ERROR", "invalid item result state")
        now = _utc_now()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status=?, outcome_code=?, retryable=?,
                       claimed_at=NULL, worker_id=NULL,
                       next_retry_at=?, last_error_code=?, last_error_message=?,
                       document_sync_status=?, document_business_status=?,
                       document_current_version=?, parser_application_status=?,
                       quality_status=?, persisted_state_json=?, updated_at=?
                   WHERE id=? AND item_status='processing'""",
                (
                    item_status,
                    outcome_code,
                    int(retryable),
                    self._next_retry_at(item.attempt_count) if retryable else None,
                    error_code,
                    _bounded(error_message),
                    state.get("sync_status"),
                    state.get("business_status"),
                    int(bool(state.get("current_version"))),
                    state.get("parser_application_status"),
                    state.get("quality_status"),
                    _state_json(state),
                    now,
                    item.id,
                ),
            )
            if cursor.rowcount != 1:
                await self.db.rollback()
                return
            if item_status == "review_required":
                await self._open_review_transaction(
                    item,
                    reason=(review_reason or "review_required")[:_MAX_REASON_LENGTH],
                    before_state=item.persisted_state,
                    after_state=state,
                    now=now,
                )
            await self._insert_audit(
                tenant_id=item.tenant_id,
                batch_id=item.batch_id,
                item_id=item.id,
                actor_id=worker_id,
                action="item_processed",
                reason=outcome_code,
                before_state={"item_status": "processing"},
                after_state={
                    "item_status": item_status,
                    "document_state": state,
                },
                request_id=None,
                commit=False,
            )
            await self._refresh_batch(item.batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    @staticmethod
    def _next_retry_at(attempt_count: int) -> str:
        delay = min(2 ** max(attempt_count - 1, 0), 60)
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    async def _document_state(
        self,
        doc: Any | None,
        evaluation: Any | None = None,
    ) -> dict[str, Any]:
        if evaluation is None and doc is not None:
            evaluation = await get_latest_evaluation(
                self.db,
                doc.tenant_id,
                doc.source_system,
                doc.external_document_id,
                doc.source_version_id,
            )
        return {
            "sync_status": getattr(doc, "sync_status", None),
            "business_status": getattr(doc, "business_status", None),
            "current_version": bool(getattr(doc, "current_version", 0)),
            "parser_application_status": getattr(
                doc, "parser_application_status", None,
            ),
            "quality_status": getattr(evaluation, "parse_quality_status", None),
            "source_version_id": getattr(doc, "source_version_id", None),
        }

    @staticmethod
    def _needs_review(doc: Any | None, evaluation: Any | None) -> bool:
        if doc is not None and (
            doc.business_status == "review_required"
            or doc.sync_status == "review_required"
            or doc.parser_application_status in {"mismatch", "legacy_unverified"}
        ):
            return True
        return bool(
            evaluation
            and evaluation.parse_quality_status in {"review_required", "failed"}
        )

    @staticmethod
    def _review_reasons(doc: Any | None, evaluation: Any | None) -> list[str]:
        reasons: list[str] = []
        if doc is not None:
            if doc.business_status == "review_required":
                reasons.append("DOCUMENT_BUSINESS_REVIEW_REQUIRED")
            if doc.sync_status == "review_required":
                reasons.append("DOCUMENT_SYNC_REVIEW_REQUIRED")
            if doc.parser_application_status in {"mismatch", "legacy_unverified"}:
                reasons.append(
                    f"PARSER_APPLICATION_{doc.parser_application_status.upper()}"
                )
        if evaluation and evaluation.parse_quality_status in {"review_required", "failed"}:
            reasons.append(
                f"PARSE_QUALITY_{evaluation.parse_quality_status.upper()}"
            )
        return reasons or ["REVIEW_REQUIRED"]

    async def _open_review_transaction(
        self,
        item: ImportItemRecord,
        *,
        reason: str,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        now: str,
    ) -> int:
        existing = await self._get_open_review(item.id)
        if existing:
            return existing.id
        cursor = await self.db.execute(
            """INSERT INTO parsing_review
               (batch_id, item_id, tenant_id, source_system,
                external_document_id, source_version_id, review_status,
                reason, before_state_json, after_state_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'review_required', ?, ?, ?, ?, ?)""",
            (
                item.batch_id,
                item.id,
                item.tenant_id,
                item.source_system,
                item.external_document_id,
                item.source_version_id,
                reason,
                _state_json(before_state),
                _state_json(after_state),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    async def _mark_review_required(
        self,
        item: ImportItemRecord,
        state: dict[str, Any],
        reason: str,
    ) -> None:
        now = _utc_now()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status='review_required', outcome_code='REVIEW_REQUIRED',
                       retryable=0, persisted_state_json=?,
                       document_sync_status=?, document_business_status=?,
                       document_current_version=?, parser_application_status=?,
                       quality_status=?, updated_at=?
                   WHERE id=? AND item_status NOT IN ('rejected', 'conflict')""",
                (
                    _state_json(state),
                    state.get("sync_status"),
                    state.get("business_status"),
                    int(bool(state.get("current_version"))),
                    state.get("parser_application_status"),
                    state.get("quality_status"),
                    now,
                    item.id,
                ),
            )
            await self._open_review_transaction(
                item,
                reason=reason[:_MAX_REASON_LENGTH],
                before_state=item.persisted_state,
                after_state=state,
                now=now,
            )
            await self._insert_audit(
                tenant_id=item.tenant_id,
                batch_id=item.batch_id,
                item_id=item.id,
                actor_id="quality-reconciler",
                action="review_required_enqueued",
                reason=reason[:_MAX_REASON_LENGTH],
                before_state=item.persisted_state,
                after_state=state,
                request_id=None,
                commit=False,
            )
            await self._refresh_batch(item.batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def _refresh_batch(self, batch_id: str, *, commit: bool = True) -> None:
        async with self.db.execute(
            "SELECT item_status, sequence, id, last_error_code, last_error_message "
            "FROM parsing_import_item WHERE batch_id=? ORDER BY sequence",
            (batch_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        counts: dict[str, int] = {}
        checkpoint_sequence = -1
        checkpoint_item_id: int | None = None
        checkpoint_open = True
        for row in rows:
            status = row["item_status"]
            counts[status] = counts.get(status, 0) + 1
            if (
                checkpoint_open
                and status in TERMINAL_ITEM_STATES
                and checkpoint_sequence == row["sequence"] - 1
            ):
                checkpoint_sequence = row["sequence"]
                checkpoint_item_id = row["id"]
            elif checkpoint_open:
                checkpoint_open = False
        if counts.get("failed", 0) or counts.get("conflict", 0) or counts.get("rejected", 0):
            status = "failed"
        elif counts.get("review_required", 0):
            status = "review_required"
        elif counts.get("pending", 0) or counts.get("processing", 0):
            status = "running" if counts.get("processing", 0) else "pending"
        else:
            status = "completed"
        error_code = None
        error_message = None
        for row in reversed(rows):
            if row["last_error_code"]:
                error_code = row["last_error_code"]
                error_message = row["last_error_message"]
                break
        await self.db.execute(
            """UPDATE parsing_import_batch
               SET status=?, checkpoint_sequence=?, checkpoint_item_id=?,
                   completed_items=?, deduplicated_items=?, conflict_items=?,
                   failed_items=?, review_required_items=?,
                   last_error_code=?, last_error_message=?, updated_at=?
               WHERE batch_id=?""",
            (
                status,
                checkpoint_sequence,
                checkpoint_item_id,
                counts.get("completed", 0),
                counts.get("deduplicated", 0),
                counts.get("conflict", 0),
                counts.get("failed", 0) + counts.get("rejected", 0),
                counts.get("review_required", 0),
                error_code,
                _bounded(error_message),
                _utc_now(),
                batch_id,
            ),
        )
        if commit:
            await self.db.commit()

    async def _insert_audit(
        self,
        *,
        tenant_id: str,
        batch_id: str | None,
        item_id: int | None,
        actor_id: str,
        action: str,
        reason: str | None,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        request_id: str | None,
        review_id: int | None = None,
        commit: bool = True,
    ) -> None:
        await self.db.execute(
            """INSERT INTO parsing_audit_event
               (tenant_id, batch_id, item_id, review_id, actor_id, action,
                reason, before_state_json, after_state_json, request_id,
                occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tenant_id,
                batch_id,
                item_id,
                review_id,
                actor_id,
                action,
                _bounded(reason, _MAX_REASON_LENGTH),
                _state_json(before_state),
                _state_json(after_state),
                request_id,
                _utc_now(),
            ),
        )
        if commit:
            await self.db.commit()

    async def list_review_queue(
        self,
        *,
        batch_id: str | None = None,
        tenant_id: str | None = None,
        source_system: str | None = None,
        principal: Any | None = None,
        limit: int = 100,
    ) -> list[ReviewRecord]:
        await self.ensure_schema()
        if principal is not None:
            if batch_id:
                batch = await self._get_batch_by_id(batch_id)
                if not batch:
                    raise HistoricalImportError("BATCH_NOT_FOUND", "Batch not found")
                if tenant_id and tenant_id != batch.tenant_id:
                    raise ImportPermissionError()
                tenant_id = batch.tenant_id
                source_system = batch.source_system
            if not tenant_id:
                tenant_id = getattr(principal, "tenant_id", None)
            if not tenant_id:
                raise ImportPermissionError()
            self._authorize(
                principal,
                tenant_id,
                source_system or getattr(principal, "source_system", "service"),
                capability="review",
                internal_allowed=False,
            )
        clauses = ["review_status='review_required'"]
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if source_system:
            clauses.append("source_system=?")
            params.append(source_system)
        params.append(max(1, min(limit, 500)))
        async with self.db.execute(
            f"""SELECT * FROM parsing_review
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, id LIMIT ?""",
            params,
        ) as cursor:
            return [_row_to_review(row) for row in await cursor.fetchall()]

    async def list_audit_events(
        self,
        *,
        tenant_id: str,
        principal: Any | None = None,
        batch_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        await self.ensure_schema()
        source_system = "service"
        if isinstance(principal, ServicePrincipal):
            source_system = principal.source_system
        self._authorize(
            principal,
            tenant_id,
            source_system,
            capability="audit",
            internal_allowed=False,
        )
        clauses = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        params.append(max(1, min(limit, 500)))
        async with self.db.execute(
            f"""SELECT * FROM parsing_audit_event
                WHERE {' AND '.join(clauses)}
                ORDER BY occurred_at, id LIMIT ?""",
            params,
        ) as cursor:
            return [_row_to_audit(row) for row in await cursor.fetchall()]

    async def _transition_document_transaction(
        self,
        doc: Any | None,
        target_status: str,
        *,
        event_status: str,
        business_status: str | None,
        clear_error: bool = True,
    ) -> Any | None:
        if doc is None:
            return None
        if doc.sync_status != target_status:
            validate_transition(doc.sync_status, target_status, "document")
        await self.db.execute(
            """UPDATE ext_document_map
               SET sync_status=?, event_status=?,
                   business_status=COALESCE(?, business_status),
                   last_error_code=?, last_error_message=?, next_retry_at=?,
                   updated_at=?, last_sync_at=?
               WHERE id=?""",
            (
                target_status,
                event_status,
                business_status,
                None if clear_error else doc.last_error_code,
                None if clear_error else doc.last_error_message,
                None,
                _utc_now(),
                _utc_now(),
                doc.id,
            ),
        )
        return await get_mapping(
            self.db,
            doc.tenant_id,
            doc.source_system,
            doc.external_document_id,
            doc.source_version_id,
        )

    async def _queue_retry_in_transaction(
        self,
        doc: Any | None,
        before: dict[str, Any],
    ) -> dict[str, Any]:
        if doc is None:
            return dict(before)
        if doc.business_status in {"disabled", "deleted", "superseded"}:
            raise ReviewStateError(
                "DOCUMENT_NOT_RETRYABLE",
                "disabled, deleted, or superseded documents cannot be retried",
            )
        current = doc.sync_status
        if current == "ready":
            # A quality review on a ready but not-current historical version is
            # moved through existing review_required before retry_wait. This
            # never changes current_version directly.
            if doc.current_version:
                raise ReviewStateError(
                    "CURRENT_VERSION_RETRY_REQUIRES_REINDEX",
                    "current version retry must use the existing reindex flow",
                )
            validate_transition(current, "review_required", "document")
            await self._transition_document_transaction(
                doc,
                "review_required",
                event_status="retry_wait",
                business_status="review_required",
            )
            doc = await get_mapping(
                self.db,
                doc.tenant_id,
                doc.source_system,
                doc.external_document_id,
                doc.source_version_id,
            )
            current = doc.sync_status
        if current != "retry_wait":
            validate_transition(current, "retry_wait", "document")
            doc = await self._transition_document_transaction(
                doc,
                "retry_wait",
                event_status="retry_wait",
                business_status="active",
            )
        return await self._document_state(doc)

    async def _reject_in_transaction(
        self,
        doc: Any | None,
        before: dict[str, Any],
    ) -> dict[str, Any]:
        if doc is None:
            return dict(before)
        current = doc.sync_status
        if current == "ready":
            target = "review_required"
        elif current == "review_required" or current == "retry_wait":
            target = "failed"
        elif current == "failed":
            target = "failed"
        elif transition_allowed(current, "failed", "document"):
            target = "failed"
        else:
            target = current
        if target != current or target == "failed":
            doc = await self._transition_document_transaction(
                doc,
                target,
                event_status="failed",
                business_status="review_required",
            )
        return await self._document_state(doc)

    async def review_item(
        self,
        review_id: int,
        *,
        decision: str,
        reason: str,
        principal: Any | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> ReviewRecord:
        await self.ensure_schema()
        if decision not in REVIEW_DECISIONS:
            raise ReviewStateError("VALIDATION_ERROR", "unsupported review decision")
        reason = reason.strip()
        if not reason or len(reason) > _MAX_REASON_LENGTH:
            raise ReviewStateError(
                "VALIDATION_ERROR", "review reason is required and bounded",
            )
        review = await self._get_review(review_id)
        if not review:
            raise ReviewStateError("REVIEW_NOT_FOUND", "Review record not found")
        self._authorize(
            principal,
            review.tenant_id,
            review.source_system,
            capability="review",
            internal_allowed=False,
        )
        if review.review_status != "review_required":
            raise ReviewStateError(
                "REVIEW_ALREADY_CLOSED", "Review record is already closed",
            )
        item = await self._get_item(review.item_id)
        if not item:
            raise ReviewStateError("ITEM_NOT_FOUND", "Batch item not found")
        actor = self._actor_id(principal, actor_id)
        request_id = request_id or str(uuid.uuid4())
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            doc = await get_mapping(
                self.db,
                review.tenant_id,
                review.source_system,
                review.external_document_id,
                review.source_version_id,
            )
            before = await self._document_state(doc)
            if decision == "reject":
                after = await self._reject_in_transaction(doc, before)
                item_status = "rejected"
                review_status = "rejected"
                outcome = "REVIEW_REJECTED"
            elif decision == "retry":
                after = await self._queue_retry_in_transaction(doc, before)
                item_status = "pending"
                review_status = "retry_queued"
                outcome = "RETRY_QUEUED_BY_REVIEW"
            else:
                # Approval never bypasses the quality gate or promotes a
                # version. It only closes the queue item with stored state.
                after = before
                item_status = "completed"
                review_status = "approved"
                outcome = "REVIEW_APPROVED_WITHOUT_PROMOTION"
            now = _utc_now()
            await self.db.execute(
                """UPDATE parsing_review
                   SET review_status=?, decision=?, operator_id=?,
                       reviewed_at=?, reason=?, before_state_json=?,
                       after_state_json=?, updated_at=?
                   WHERE id=? AND review_status='review_required'""",
                (
                    review_status,
                    decision,
                    actor,
                    now,
                    reason,
                    _state_json(before),
                    _state_json(after),
                    now,
                    review_id,
                ),
            )
            await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status=?, outcome_code=?, retryable=?,
                       claimed_at=NULL, worker_id=NULL,
                       persisted_state_json=?, document_sync_status=?,
                       document_business_status=?, document_current_version=?,
                       parser_application_status=?, quality_status=?,
                       updated_at=? WHERE id=?""",
                (
                    item_status,
                    outcome,
                    int(decision == "retry"),
                    _state_json(after),
                    after.get("sync_status"),
                    after.get("business_status"),
                    int(bool(after.get("current_version"))),
                    after.get("parser_application_status"),
                    after.get("quality_status"),
                    now,
                    item.id,
                ),
            )
            await self._insert_audit(
                tenant_id=review.tenant_id,
                batch_id=review.batch_id,
                item_id=review.item_id,
                review_id=review_id,
                actor_id=actor,
                action=f"review_{decision}",
                reason=reason,
                before_state=before,
                after_state=after,
                request_id=request_id,
                commit=False,
            )
            await self._refresh_batch(review.batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return (await self._get_review(review_id)) or review

    async def refresh_review_queue(self, batch_id: str) -> list[ReviewRecord]:
        """Materialize parser/quality failures as review_required rows."""
        await self.ensure_schema()
        items = await self.list_items(batch_id)
        for item in items:
            if item.item_status in {"rejected", "conflict", "deduplicated"}:
                continue
            doc = await get_mapping(
                self.db,
                item.tenant_id,
                item.source_system,
                item.external_document_id,
                item.source_version_id,
            )
            evaluation = await get_latest_evaluation(
                self.db,
                item.tenant_id,
                item.source_system,
                item.external_document_id,
                item.source_version_id,
            )
            if not self._needs_review(doc, evaluation):
                continue
            state = await self._document_state(doc, evaluation)
            reasons = self._review_reasons(doc, evaluation)
            await self._mark_review_required(
                item, state, ";".join(reasons),
            )
        await self._refresh_batch(batch_id)
        return await self.list_review_queue(batch_id=batch_id)

    async def retry_failed_item(
        self,
        batch_id: str,
        item_id: int,
        *,
        principal: Any | None = None,
        reason: str = "retry_requested",
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> ImportItemRecord:
        """Queue a failed/review item through the existing retry_wait state."""
        await self.ensure_schema()
        item = await self._get_item(item_id)
        if not item or item.batch_id != batch_id:
            raise HistoricalImportError("ITEM_NOT_FOUND", "Batch item not found")
        self._authorize(
            principal,
            item.tenant_id,
            item.source_system,
            capability="review",
            internal_allowed=False,
        )
        if item.item_status not in RETRYABLE_ITEM_STATES:
            raise ReviewStateError(
                "ITEM_NOT_RETRYABLE",
                f"item status {item.item_status} cannot be retried",
            )
        reason = reason.strip()
        if not reason or len(reason) > _MAX_REASON_LENGTH:
            raise ReviewStateError(
                "VALIDATION_ERROR", "retry reason is required and bounded",
            )
        review = await self._get_open_review(item.id)
        actor = self._actor_id(principal, actor_id)
        request_id = request_id or str(uuid.uuid4())
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            doc = await get_mapping(
                self.db,
                item.tenant_id,
                item.source_system,
                item.external_document_id,
                item.source_version_id,
            )
            before = await self._document_state(doc)
            after = await self._queue_retry_in_transaction(doc, before)
            now = _utc_now()
            await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status='pending', outcome_code='RETRY_QUEUED',
                       retryable=1, next_retry_at=NULL, claimed_at=NULL,
                       worker_id=NULL, last_error_code=NULL,
                       last_error_message=NULL, persisted_state_json=?,
                       document_sync_status=?, document_business_status=?,
                       document_current_version=?, parser_application_status=?,
                       quality_status=?, updated_at=? WHERE id=?""",
                (
                    _state_json(after),
                    after.get("sync_status"),
                    after.get("business_status"),
                    int(bool(after.get("current_version"))),
                    after.get("parser_application_status"),
                    after.get("quality_status"),
                    now,
                    item.id,
                ),
            )
            if review:
                await self.db.execute(
                    """UPDATE parsing_review
                       SET review_status='retry_queued', decision='retry',
                           operator_id=?, reviewed_at=?, after_state_json=?,
                           updated_at=?
                       WHERE id=? AND review_status='review_required'""",
                    (
                        actor,
                        now,
                        _state_json(after),
                        now,
                        review.id,
                    ),
                )
            await self._insert_audit(
                tenant_id=item.tenant_id,
                batch_id=batch_id,
                item_id=item.id,
                review_id=review.id if review else None,
                actor_id=actor,
                action="retry_queued",
                reason=reason,
                before_state=before,
                after_state=after,
                request_id=request_id,
                commit=False,
            )
            await self._refresh_batch(batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return (await self._get_item(item.id)) or item

    async def retry_after_review(
        self,
        review_id: int,
        *,
        reason: str,
        principal: Any | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> ImportItemRecord:
        """Retry a closed review while retaining the closed decision history."""
        review = await self._get_review(review_id)
        if not review:
            raise ReviewStateError("REVIEW_NOT_FOUND", "Review record not found")
        if review.review_status not in {"rejected", "approved"}:
            raise ReviewStateError(
                "REVIEW_NOT_RETRYABLE",
                "review must be closed before retry",
            )
        item = await self._get_item(review.item_id)
        if not item:
            raise ReviewStateError("ITEM_NOT_FOUND", "Batch item not found")
        self._authorize(
            principal,
            review.tenant_id,
            review.source_system,
            capability="review",
            internal_allowed=False,
        )
        reason = reason.strip()
        if not reason or len(reason) > _MAX_REASON_LENGTH:
            raise ReviewStateError(
                "VALIDATION_ERROR", "retry reason is required and bounded",
            )
        actor = self._actor_id(principal, actor_id)
        request_id = request_id or str(uuid.uuid4())
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            doc = await get_mapping(
                self.db,
                review.tenant_id,
                review.source_system,
                review.external_document_id,
                review.source_version_id,
            )
            before = await self._document_state(doc)
            after = await self._queue_retry_in_transaction(doc, before)
            now = _utc_now()
            await self.db.execute(
                """UPDATE parsing_import_item
                   SET item_status='pending', outcome_code='RETRY_QUEUED',
                       retryable=1, claimed_at=NULL, worker_id=NULL,
                       last_error_code=NULL, last_error_message=NULL,
                       persisted_state_json=?, document_sync_status=?,
                       document_business_status=?, document_current_version=?,
                       parser_application_status=?, quality_status=?, updated_at=?
                   WHERE id=?""",
                (
                    _state_json(after),
                    after.get("sync_status"),
                    after.get("business_status"),
                    int(bool(after.get("current_version"))),
                    after.get("parser_application_status"),
                    after.get("quality_status"),
                    now,
                    item.id,
                ),
            )
            await self._insert_audit(
                tenant_id=review.tenant_id,
                batch_id=review.batch_id,
                item_id=review.item_id,
                review_id=review.id,
                actor_id=actor,
                action="retry_after_review",
                reason=reason,
                before_state=before,
                after_state=after,
                request_id=request_id,
                commit=False,
            )
            await self._refresh_batch(review.batch_id, commit=False)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return (await self._get_item(item.id)) or item

    async def replay_batch(
        self,
        batch_id: str,
        *,
        principal: Any | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Replay persisted batch state and audit events without recomputation.

        In particular, this method does not inspect or count citations. The
        stored business/parser/quality state is the source of truth for replay.
        """
        await self.ensure_schema()
        batch = await self.get_batch(batch_id)
        if principal is not None:
            self._authorize(
                principal,
                batch.tenant_id,
                batch.source_system,
                capability="audit",
                internal_allowed=False,
            )
        items = await self.list_items(batch_id, limit=limit)
        async with self.db.execute(
            """SELECT * FROM parsing_audit_event
               WHERE tenant_id=? AND batch_id=?
               ORDER BY occurred_at, id LIMIT ?""",
            (batch.tenant_id, batch_id, max(1, min(limit, 500))),
        ) as cursor:
            audit = [_row_to_audit(row) for row in await cursor.fetchall()]
        return {
            "batch": batch.to_dict(),
            "items": [item.to_dict() for item in items],
            "audit": [event.to_dict() for event in audit],
        }

    async def list_audit_events_for_batch(
        self,
        batch_id: str,
        *,
        principal: Any | None = None,
        limit: int = 500,
    ) -> list[AuditRecord]:
        batch = await self.get_batch(batch_id)
        return await self.list_audit_events(
            tenant_id=batch.tenant_id,
            principal=principal,
            batch_id=batch_id,
            limit=limit,
        )
