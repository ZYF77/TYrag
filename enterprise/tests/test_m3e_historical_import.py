"""M3-E internal historical-import adapter tests.

These tests cover the Enterprise service boundary only. They are not live
RAGFlow/object-storage integration evidence; M2 remains blocked without the
real external environment and corpus.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.parsing.historical_import import (
    BatchConflictError,
    HistoricalImportService,
    ImportPermissionError,
)
from enterprise.gateway.quality import models as quality_models
from enterprise.gateway.quality.routing import route_document
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    get_mapping,
    init_db,
    insert_mapping,
    update_mapping_status,
    update_parser_application,
)
from enterprise.gateway.sync.sync_service import (
    RetryableDocumentSyncError,
    TerminalDocumentSyncError,
)


def _item(
    content: bytes = b"history",
    *,
    document_id: str = "DOC-1",
    version: str = "v1",
    tenant: str = "tenant-a",
    event_id: str | None = None,
) -> dict:
    return {
        "tenantId": tenant,
        "sourceSystem": "EAM",
        "externalDocumentId": document_id,
        "sourceVersionId": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": f"{document_id}.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": "history", "objectKey": f"{document_id}/{version}.pdf"},
        "metadata": {
            "document_type": "manual",
            "page_count": 1,
            "allow_group_ids": ["maintenance"],
            "department_id": "dept-a",
            "security_level": 1,
        },
        **({"eventId": event_id} if event_id else {}),
    }


def _principal(
    tenant: str = "tenant-a",
    capabilities: tuple[str, ...] = ("review", "audit", "upload"),
) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant,
        business_user_id="operator-1",
        subject="operator-1",
        department_ids=("dept-a",),
        group_ids=("maintenance",),
        security_level=3,
        capabilities=capabilities,
    )


async def _success_processor(db, calls: list[str], failures: int = 0):
    async def process(event):
        calls.append(event.event_id)
        if len(calls) <= failures:
            raise RetryableDocumentSyncError("SOURCE_UNAVAILABLE", "source unavailable")
        payload = json.loads(event.payload)
        doc = await get_mapping(
            db,
            event.tenant_id,
            event.source_system,
            event.external_document_id,
            event.source_version_id,
        )
        if doc is None:
            doc = await insert_mapping(
                db,
                ExtDocumentMap(
                    tenant_id=event.tenant_id,
                    source_system=event.source_system,
                    external_document_id=event.external_document_id,
                    source_version_id=event.source_version_id,
                    event_id=event.event_id,
                    sha256=payload["sha256"],
                    file_name=payload["fileName"],
                    media_type=payload["mediaType"],
                    document_type=payload["metadata"].get("document_type"),
                    department_id=payload["metadata"].get("department_id"),
                    security_level=payload["metadata"].get("security_level"),
                    allow_group_ids=json.dumps(
                        payload["metadata"].get("allow_group_ids", []),
                    ),
                    bucket=payload["source"]["bucket"],
                    object_key=payload["source"]["objectKey"],
                ),
            )
        await update_mapping_status(
            db,
            doc,
            "ready",
            event_status="completed",
            pipeline_status="DONE",
            business_status="active",
            current_version=0,
        )
        await update_parser_application(
            db,
            doc,
            status="executed",
            profile="pdf_deepdoc_v1",
            profile_version="1",
            expected_json='{"profile":"pdf_deepdoc_v1"}',
            configured_json='{"profile":"pdf_deepdoc_v1"}',
            executed_json='{"profile":"pdf_deepdoc_v1"}',
        )
        return doc, False

    return process


async def _review_failure_processor(db, calls: list[str]):
    async def process(event):
        calls.append(event.event_id)
        payload = json.loads(event.payload)
        doc = await get_mapping(
            db,
            event.tenant_id,
            event.source_system,
            event.external_document_id,
            event.source_version_id,
        )
        if doc is None:
            doc = await insert_mapping(
                db,
                ExtDocumentMap(
                    tenant_id=event.tenant_id,
                    source_system=event.source_system,
                    external_document_id=event.external_document_id,
                    source_version_id=event.source_version_id,
                    event_id=event.event_id,
                    sha256=payload["sha256"],
                    file_name=payload["fileName"],
                ),
            )
        await update_mapping_status(
            db,
            doc,
            "failed",
            event_status="failed",
            error_code="DOCUMENT_PARSE_FAILED",
            error_message="parse result requires review",
            business_status="review_required",
        )
        raise TerminalDocumentSyncError(
            "DOCUMENT_PARSE_FAILED", "parse result requires review",
        )

    return process


@pytest.mark.asyncio
async def test_batch_contract_checkpoint_and_idempotent_replay():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _success_processor(db, calls))
    items = [_item(document_id="DOC-1"), _item(b"two", document_id="DOC-2")]

    first = await service.submit_batch("batch-1", items)
    replay = await service.submit_batch("batch-1", items)
    assert first.manifest_hash == replay.manifest_hash
    assert first.total_items == 2

    partial = await service.run_batch("batch-1", limit=1, worker_id="w1")
    assert partial.status == "pending"
    assert partial.checkpoint_sequence == 0
    assert partial.completed_items == 1
    assert len(calls) == 1

    completed = await service.run_batch("batch-1", worker_id="w2")
    assert completed.status == "completed"
    assert completed.checkpoint_sequence == 1
    assert len(calls) == 2
    assert len(await service.list_items("batch-1")) == 2

    duplicate_batch = await service.submit_batch("batch-2", [items[0]])
    duplicate_item = (await service.list_items(duplicate_batch.batch_id))[0]
    assert duplicate_item.item_status == "deduplicated"
    assert duplicate_batch.status == "completed"
    assert len(calls) == 2
    await db.close()


@pytest.mark.asyncio
async def test_checkpoint_recovery_reclaims_interrupted_item():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _success_processor(db, calls))
    await service.submit_batch("batch-recover", [_item()])
    claimed = await service._claim_next_item("batch-recover", "crashed-worker")
    assert claimed is not None
    await db.execute(
        "UPDATE parsing_import_item SET claimed_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", claimed.id),
    )
    await db.commit()

    assert await service.recover_checkpoint("batch-recover", stale_after_seconds=0) == 1
    recovered = (await service.list_items("batch-recover"))[0]
    assert recovered.item_status == "pending"
    assert recovered.last_error_code == "BATCH_WORKER_INTERRUPTED"
    await service.run_batch("batch-recover", worker_id="new-worker")
    assert (await service.get_batch("batch-recover")).status == "completed"
    assert len(calls) == 1
    await db.close()


@pytest.mark.asyncio
async def test_duplicate_version_and_file_conflicts_are_safe():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _success_processor(db, calls))
    duplicate = _item()
    batch = await service.submit_batch(
        "batch-duplicates", [duplicate, duplicate, _item(document_id="DOC-2")],
    )
    items = await service.list_items(batch.batch_id)
    assert items[1].item_status == "deduplicated"
    assert items[1].outcome_code == "DUPLICATE_VERSION"
    assert items[2].item_status == "conflict"
    assert items[2].outcome_code == "DOCUMENT_DUPLICATE_FILE"

    version_conflict = await service.submit_batch(
        "batch-version-conflict",
        [_item(content=b"v1"), _item(content=b"v2")],
    )
    version_items = await service.list_items(version_conflict.batch_id)
    assert version_items[1].item_status == "conflict"
    assert version_items[1].outcome_code == "DOCUMENT_VERSION_CONFLICT"
    assert (await service.get_batch(version_conflict.batch_id)).status == "failed"
    await db.close()


@pytest.mark.asyncio
async def test_changed_manifest_replay_is_rejected():
    db = await init_db(":memory:")
    service = HistoricalImportService(db)
    items = [_item()]
    await service.submit_batch("batch-idempotency", items, idempotency_key="manifest-1")
    with pytest.raises(BatchConflictError) as error:
        await service.submit_batch(
            "batch-idempotency", [_item(content=b"changed")],
            idempotency_key="manifest-1",
        )
    assert error.value.code == "BATCH_IDEMPOTENCY_CONFLICT"
    await db.close()


@pytest.mark.asyncio
async def test_failed_item_can_be_retried_without_duplicate_processing():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(
        db, await _success_processor(db, calls, failures=1),
    )
    await service.submit_batch("batch-retry", [_item()])
    first = await service.run_batch("batch-retry", worker_id="retry-worker")
    assert first.status == "failed"
    failed = (await service.list_items("batch-retry"))[0]
    assert failed.item_status == "failed"
    assert failed.retryable is True

    reviewer = _principal()
    queued = await service.retry_failed_item(
        "batch-retry", failed.id, principal=reviewer, reason="source restored",
    )
    assert queued.item_status == "pending"
    await service.run_batch("batch-retry", worker_id="retry-worker-2")
    assert (await service.get_batch("batch-retry")).status == "completed"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    await db.close()


@pytest.mark.asyncio
async def test_review_queue_rejection_audit_and_permission_isolation():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _review_failure_processor(db, calls))
    await service.submit_batch("batch-review", [_item()])
    batch = await service.run_batch("batch-review", worker_id="review-worker")
    assert batch.status == "review_required"
    queue = await service.list_review_queue(batch_id="batch-review")
    assert len(queue) == 1
    review = queue[0]
    assert review.review_status == "review_required"
    assert "DOCUMENT_BUSINESS_REVIEW_REQUIRED" in review.reason

    with pytest.raises(ImportPermissionError):
        await service.list_review_queue(
            batch_id="batch-review", principal=_principal("tenant-b")
        )
    with pytest.raises(ImportPermissionError):
        await service.review_item(
            review.id,
            decision="reject",
            reason="not my tenant",
            principal=_principal("tenant-b"),
        )
    with pytest.raises(ImportPermissionError):
        await service.review_item(
            review.id,
            decision="reject",
            reason="missing review capability",
            principal=_principal(capabilities=("upload",)),
        )

    reviewed = await service.review_item(
        review.id,
        decision="reject",
        reason="OCR evidence is not acceptable",
        principal=_principal(),
        request_id="review-request-1",
    )
    assert reviewed.review_status == "rejected"
    assert reviewed.operator_id == "operator-1"
    assert reviewed.reviewed_at
    assert reviewed.before_state["sync_status"] == "failed"
    assert reviewed.after_state["sync_status"] == "failed"
    assert reviewed.after_state["business_status"] == "review_required"

    doc = await get_mapping(db, "tenant-a", "EAM", "DOC-1", "v1")
    assert doc is not None
    assert doc.sync_status == "failed"
    assert doc.business_status == "review_required"
    audits = await service.list_audit_events_for_batch(
        "batch-review", principal=_principal(),
    )
    assert any(event.action == "review_reject" for event in audits)
    assert all(event.actor_id for event in audits)
    await db.close()


@pytest.mark.asyncio
async def test_review_reject_then_retry_preserves_history_and_reprocesses():
    db = await init_db(":memory:")
    calls: list[str] = []
    first_processor = await _review_failure_processor(db, calls)
    service = HistoricalImportService(db, first_processor)
    await service.submit_batch("batch-review-retry", [_item()])
    await service.run_batch("batch-review-retry", worker_id="review-worker")
    review = (await service.list_review_queue(batch_id="batch-review-retry"))[0]
    await service.review_item(
        review.id,
        decision="reject",
        reason="reject current parse",
        principal=_principal(),
    )
    queued = await service.retry_after_review(
        review.id,
        reason="corrected source profile",
        principal=_principal(),
    )
    assert queued.item_status == "pending"
    doc = await get_mapping(db, "tenant-a", "EAM", "DOC-1", "v1")
    assert doc is not None
    assert doc.sync_status == "retry_wait"
    assert doc.business_status == "active"

    # The callback is replaced only for this unit-level retry path. The event
    # identity remains the original event id, so a real SyncService replay is
    # idempotent as well.
    service.processor = await _success_processor(db, calls)
    await service.run_batch("batch-review-retry", worker_id="retry-worker")
    assert (await service.get_batch("batch-review-retry")).status == "completed"
    assert not await service.list_review_queue(batch_id="batch-review-retry")
    assert calls[0] == calls[1]
    await db.close()


@pytest.mark.asyncio
async def test_historical_replay_uses_persisted_business_state_not_citations():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _review_failure_processor(db, calls))
    await service.submit_batch("batch-replay", [_item()])
    await service.run_batch("batch-replay", worker_id="replay-worker")
    before = await service.replay_batch("batch-replay")
    assert before["items"][0]["documentState"]["sync_status"] == "failed"
    assert "citations" not in before["items"][0]

    doc = await get_mapping(db, "tenant-a", "EAM", "DOC-1", "v1")
    assert doc is not None
    await update_mapping_status(
        db, doc, "ready", event_status="completed", business_status="active",
    )
    replay = await service.replay_batch("batch-replay")
    assert replay["items"][0]["documentState"]["sync_status"] == "failed"
    assert replay["items"][0]["documentState"]["business_status"] == "review_required"
    await db.close()


@pytest.mark.asyncio
async def test_parser_application_state_is_persisted_in_batch_replay():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _success_processor(db, calls))
    await service.submit_batch("batch-parser-state", [_item()])
    await service.run_batch("batch-parser-state")
    replay = await service.replay_batch("batch-parser-state")
    state = replay["items"][0]["documentState"]
    assert state["parser_application_status"] == "executed"
    assert state["sync_status"] == "ready"
    await db.close()


@pytest.mark.asyncio
async def test_quality_review_status_materializes_review_required_queue():
    db = await init_db(":memory:")
    calls: list[str] = []
    service = HistoricalImportService(db, await _success_processor(db, calls))
    await service.submit_batch("batch-quality-review", [_item()])
    await service.run_batch("batch-quality-review")
    doc = await get_mapping(db, "tenant-a", "EAM", "DOC-1", "v1")
    assert doc is not None
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=None,
        ragflow_document_id=None,
        routing=route_document(media_type=doc.media_type, file_name=doc.file_name),
    )
    await quality_models.complete_evaluation(
        db,
        evaluation.id,
        parse_quality_status="review_required",
        quality_reasons=["PAGE_COVERAGE_BELOW_MIN"],
        metrics_json={"parse_success": True},
        parse_repeatability_hash="parse-hash",
        e2e_repeatability_hash="e2e-hash",
        artifact_hash="artifact-hash",
        enterprise_commit="test",
        enterprise_worktree_dirty=False,
        ragflow_source_tag="v0.26.4",
        ragflow_source_commit="test",
        thresholds_version="1",
        thresholds_digest="digest",
    )
    queue = await service.refresh_review_queue("batch-quality-review")
    assert len(queue) == 1
    assert "PARSE_QUALITY_REVIEW_REQUIRED" in queue[0].reason
    item = (await service.list_items("batch-quality-review"))[0]
    assert item.item_status == "review_required"
    assert item.quality_status == "review_required"
    await db.close()
