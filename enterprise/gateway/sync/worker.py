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
_TERMINAL_PIPELINE = frozenset({"DONE", "3", "FAIL", "4", "CANCEL", "2"})


def _pipeline_incomplete(pipeline_status: str | None) -> bool:
    return str(pipeline_status or "").upper() not in _TERMINAL_PIPELINE


class OutboxWorker:
    def __init__(self, service: SyncService, worker_id: str | None = None) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    async def run_once(self, limit: int = 1) -> int:
        async with self.service.gateway.transaction(write=True) as conn:
            events = await claim_outbox(conn, self.worker_id, limit)
        for event in events:
            try:
                await self.service.process_event(event)
                async with self.service.gateway.transaction(write=True) as conn:
                    await mark_outbox_done(conn, event)
            except DocumentSyncError as e:
                async with self.service.gateway.transaction(write=True) as conn:
                    if e.retryable and event.attempts < event.max_attempts:
                        await mark_outbox_retry(
                            conn, event, e.code, str(e),
                        )
                    else:
                        await mark_outbox_failed(
                            conn, event, e.code, str(e),
                        )
                if not (e.retryable and event.attempts < event.max_attempts):
                    try:
                        await self.service.finalize_outbox_exhausted(
                            event, e.code, str(e), e.retryable,
                        )
                    except Exception:
                        logger.exception(
                            "Outbox exhausted finalization failed event_id=%s",
                            event.event_id,
                        )
            except Exception:
                logger.exception("Outbox processing failed event_id=%s", event.event_id)
                async with self.service.gateway.transaction(write=True) as conn:
                    await mark_outbox_failed(
                        conn, event, "INTERNAL_ERROR",
                        "服务开小差了，请稍后重试。",
                    )
                try:
                    await self.service.finalize_outbox_exhausted(
                        event, "INTERNAL_ERROR", "服务开小差了，请稍后重试。",
                    )
                except Exception:
                    logger.exception(
                        "Outbox exhausted finalization failed event_id=%s",
                        event.event_id,
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
        async with self.service.gateway.transaction(write=False) as conn:
            mappings = await list_mappings(
                conn,
                statuses=list(IN_PROGRESS_STATUSES),
                limit=limit,
                ascending=True,
            )
            ready_rows = await list_mappings(
                conn,
                statuses=["ready"],
                limit=limit,
                ascending=True,
            )
        to_refresh = list(mappings)
        seen = {doc.id for doc in to_refresh if doc.id is not None}
        for doc in ready_rows:
            if doc.id in seen:
                continue
            if _pipeline_incomplete(doc.pipeline_status):
                to_refresh.append(doc)
                if doc.id is not None:
                    seen.add(doc.id)
        updated = 0
        for doc in to_refresh:
            before = doc.sync_status
            await self.service.refresh_status(doc)
            if doc.sync_status != before:
                updated += 1
        updated += await self.service.reconcile_missing_ragflow_documents()
        return updated

    async def run_forever(self, interval_seconds: float = 10.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Status reconciler iteration failed")
            await asyncio.sleep(interval_seconds)
