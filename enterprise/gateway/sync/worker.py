"""Background outbox consumer and RAGFlow status reconciler."""
import asyncio
import logging
import uuid

from enterprise.gateway.sync.models import (
    claim_outbox,
    list_mappings,
    mark_outbox_done,
    mark_outbox_failed,
    mark_outbox_retry,
)
from enterprise.gateway.sync.sync_service import (
    DocumentSyncError,
    SyncService,
)

logger = logging.getLogger(__name__)

IN_PROGRESS_STATUSES = {
    "registered", "queued", "parsing", "indexing", "validating",
    "review_required", "tracking",
}


class OutboxWorker:
    def __init__(self, service: SyncService, worker_id: str | None = None) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    async def run_once(self, limit: int = 1) -> int:
        events = await claim_outbox(self.service.db, self.worker_id, limit)
        for event in events:
            try:
                await self.service.process_event(event)
                await mark_outbox_done(self.service.db, event)
            except DocumentSyncError as e:
                if e.retryable and event.attempts < event.max_attempts:
                    await mark_outbox_retry(
                        self.service.db, event, e.code, str(e),
                    )
                else:
                    await mark_outbox_failed(
                        self.service.db, event, e.code, str(e),
                    )
            except Exception:
                logger.exception("Outbox processing failed event_id=%s", event.event_id)
                await mark_outbox_failed(
                    self.service.db, event, "INTERNAL_ERROR",
                    "Unexpected outbox failure",
                )
        return len(events)

    async def run_forever(self, interval_seconds: float = 2.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Outbox worker iteration failed")
            await asyncio.sleep(interval_seconds)


class StatusReconciler:
    def __init__(self, service: SyncService) -> None:
        self.service = service

    async def run_once(self, limit: int = 100) -> int:
        mappings = await list_mappings(
            self.service.db,
            statuses=list(IN_PROGRESS_STATUSES),
            limit=limit,
            ascending=True,
        )
        updated = 0
        for doc in mappings:
            if doc.sync_status not in IN_PROGRESS_STATUSES:
                continue
            before = doc.sync_status
            await self.service.refresh_status(doc)
            if doc.sync_status != before:
                updated += 1
        return updated

    async def run_forever(self, interval_seconds: float = 10.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Status reconciler iteration failed")
            await asyncio.sleep(interval_seconds)
