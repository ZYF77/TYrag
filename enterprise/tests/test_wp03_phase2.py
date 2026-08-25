"""WP-03 Phase 2 tests: routing, persistence, worker, gate, and APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("ENTERPRISE_TEST_MODE", "1")
os.environ.setdefault("ENTERPRISE_SYNC_AUTH_ENABLED", "false")

from enterprise.gateway.app import app  # noqa: E402
from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.quality import models as quality_models  # noqa: E402
from enterprise.gateway.quality.gate import enforce_quality_gate  # noqa: E402
from enterprise.gateway.quality.metrics import metrics  # noqa: E402
from enterprise.gateway.quality.routing import (  # noqa: E402
    route_document,
    route_document_for_mapping,
)
from enterprise.gateway.quality.worker import (  # noqa: E402
    QualityEvaluationService,
    QualityReconciler,
    QualityEvaluationWorker,
)
from enterprise.gateway.query import acl_store  # noqa: E402
from enterprise.gateway.query import router as query_router  # noqa: E402
from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub  # noqa: E402
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    OutboxEvent,
    init_db,
    insert_mapping,
    update_mapping_status,
    update_parser_application,
)
from enterprise.gateway.sync.ragflow_document_client import (  # noqa: E402
    RAGFlowAPIError,
    RAGFlowDocumentStub,
)
from enterprise.gateway.sync.readiness import (  # noqa: E402
    document_candidate_readiness,
)
from enterprise.gateway.sync.source_adapter import SourceStub  # noqa: E402
from enterprise.gateway.sync.sync_service import SyncService  # noqa: E402
from enterprise.scripts.wp03.backfill_quality import _run as backfill_run  # noqa: E402

SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"

VALID_METADATA = {
    "schema_version": 1,
    "tenant_id": "customer-a",
    "external_document_id": "DOC-1",
    "source_system": "DEMO",
    "equipment_id": "EQ-001",
    "document_type": "manual",
    "document_version": "v1",
    "department_id": "dept-eng",
    "security_level": 3,
    "business_status": "active",
}


def _make_token(
    tenant: str = "customer-a",
    user: str = "biz-user-001",
) -> str:
    now = int(time.time())
    claims = {
        "sub": user,
        "tenant": tenant,
        "name": user,
        "department": ["d10"],
        "roles": ["end_user"],
        "groups": ["maintenance"],
        "security_level": 2,
        "iat": now - 60,
        "exp": now + 3600,
        "iss": "https://auth.example.com",
        "aud": "tyrag-gateway",
    }
    return jwt.encode(claims, SHARED_SECRET, algorithm="HS256")


def _payload_for(content: bytes, doc_id: str = "DOC-1", version: str = "v1") -> dict:
    return {
        "eventId": f"evt-{doc_id}-{version}-{uuid.uuid4().hex[:6]}",
        "eventType": "upsert",
        "sourceSystem": "DEMO",
        "externalDocumentId": doc_id,
        "sourceVersionId": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": f"{doc_id}.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": "docs", "objectKey": f"{doc_id}/{version}.pdf"},
        "metadata": {
            **VALID_METADATA,
            "tenant_id": "customer-a",
            "page_count": 1,
        },
        "batchId": None,
    }


def _make_event(content: bytes, doc_id: str = "DOC-1", version: str = "v1") -> OutboxEvent:
    payload = _payload_for(content, doc_id, version)
    return OutboxEvent(
        event_id=payload["eventId"],
        event_type="upsert",
        tenant_id="customer-a",
        source_system="DEMO",
        external_document_id=doc_id,
        source_version_id=version,
        payload=json.dumps(payload),
        batch_id=None,
        max_attempts=5,
    )


@pytest.fixture
def phase2_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase2.db")
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "false")
    monkeypatch.setenv("JWT_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("JWT_AUDIENCE", "tyrag-gateway")
    monkeypatch.setenv("JWT_ENABLE_HS", "true")
    monkeypatch.setenv("JWT_ALLOWED_ALGS", "HS256")
    monkeypatch.setenv("JWT_JWKS_URL", "")
    monkeypatch.setenv("JWT_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("ENTERPRISE_DB_PATH", db_path)
    monkeypatch.setenv("ENTERPRISE_SYNC_DB_PATH", db_path)
    monkeypatch.setenv("ENTERPRISE_QUALITY_GATE_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_QUALITY_STRICT_MODE", "true")
    monkeypatch.setenv("ENTERPRISE_QUALITY_DEMO_WARN_MODE", "false")
    return db_path


@pytest_asyncio.fixture
async def isolated_phase2_db(phase2_env):
    import enterprise.gateway.app as app_module
    import enterprise.gateway.quality.router as quality_router

    if app_module._db is not None:
        await app_module._db.close()
        app_module._db = None
    db = await init_db(phase2_env)
    repo = ExtUserMapRepo(db_path=phase2_env)
    await repo.ensure_table()
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id="customer-a",
            business_subject="biz-user-001",
            business_user_id="biz-user-001",
            mapping_strategy="B",
        )
    )
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id="customer-a",
            business_subject="biz-user-002",
            business_user_id="biz-user-002",
            mapping_strategy="B",
        )
    )
    await repo.close()
    query_router._query_stub = None
    app_module.app.dependency_overrides[app_module.get_db] = lambda: db
    app_module.app.dependency_overrides[query_router.get_db] = lambda: db
    app_module.app.dependency_overrides[quality_router.get_db] = lambda: db
    try:
        yield db
    finally:
        app_module.app.dependency_overrides.pop(app_module.get_db, None)
        app_module.app.dependency_overrides.pop(query_router.get_db, None)
        app_module.app.dependency_overrides.pop(quality_router.get_db, None)
        await db.close()


async def _insert_ready_document(
    db,
    *,
    doc_id: str,
    tenant_id: str = "customer-a",
    allowed_users: tuple[str, ...] = ("biz-user-001",),
    allow_groups: tuple[str, ...] = ("maintenance",),
    ragflow_document_id: str = "doc-1",
):
    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system="DEMO",
        external_document_id=doc_id,
        source_version_id="v1",
        event_id=str(uuid.uuid4()),
        sha256=hashlib.sha256(b"ready").hexdigest(),
        file_name="manual.pdf",
        ragflow_dataset_id="ds-1",
        ragflow_document_id=ragflow_document_id,
        sync_status="ready",
        media_type="application/pdf",
        department_id="d10",
        security_level=2,
        allow_group_ids=json.dumps(list(allow_groups)),
        deny_group_ids="[]",
    )
    doc = await insert_mapping(db, doc)
    await update_mapping_status(
        db,
        doc,
        "ready",
        pipeline_status="DONE",
        business_status="active",
        current_version=1,
    )
    for user in allowed_users:
        await acl_store.grant(
            db,
            tenant_id=tenant_id,
            external_document_id=doc_id,
            business_user_id=user,
        )
    return doc


async def _create_evaluation(
    db,
    doc,
    *,
    quality_status: str = "passed",
    state: str = "completed",
    reasons: list[str] | None = None,
    evaluation_version: str = "1",
):
    routing = route_document(
        media_type=doc.media_type,
        file_name=doc.file_name,
        source_system=doc.source_system,
    )
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=doc.ragflow_dataset_id,
        ragflow_document_id=doc.ragflow_document_id,
        routing=routing,
        evaluation_version=evaluation_version,
    )
    if state == "completed":
        await quality_models.complete_evaluation(
            db,
            evaluation.id,
            parse_quality_status=quality_status,
            quality_reasons=reasons or ["PASSED"],
            metrics_json={
                "parse_success": quality_status != "failed",
                "chunk_count": 1,
                "page_coverage": 1.0,
                "position_coverage": 1.0,
                "effective_text_coverage": 1.0,
                "garbled_char_ratio": 0.0,
                "table_recall": None,
                "key_field_accuracy": None,
                "citation_page_accuracy": None,
                "image_chunk_count": 0,
                "out_of_range_page_count": 0,
                "required_capabilities": ["text", "position"],
                "quality_expectations": {"declarations_complete": True},
                "parserApplication": {
                    "state": "executed",
                    "readbackMatch": True,
                },
            },
            parse_repeatability_hash="p",
            e2e_repeatability_hash="e",
            artifact_hash="a",
            enterprise_commit="test",
            enterprise_worktree_dirty=False,
            ragflow_source_tag="v0.26.4",
            ragflow_source_commit="test",
            thresholds_version="1",
            thresholds_digest="d",
        )
    return evaluation


def test_routing_is_deterministic_and_auditable():
    first = route_document(
        media_type="application/pdf", file_name="scan_001.pdf",
        document_type="scan", source_system="EAM",
    )
    second = route_document(
        media_type="application/pdf", file_name="scan_001.pdf",
        document_type="scan", source_system="EAM",
    )
    assert first == second
    assert first["category"] == "scanned_document"
    assert first["routing_policy_version"] == "2"
    assert first["selected_parser_profile"] == "pdf_deepdoc_v1"
    assert first["api_application_status"] == "selected"
    override = route_document(
        media_type="application/pdf", file_name="manual.pdf",
        manual_profile="tabular_table_v1",
    )
    assert override["whether_manual_override"] is False
    assert override["client_override_ignored"] is True
    assert override["selected_parser_profile"] == "pdf_deepdoc_v1"
    assert "CLIENT_PROFILE_OVERRIDE_IGNORED" in override["routing_reasons"]


def test_persisted_profile_version_is_reused_for_reprocessing():
    doc = SimpleNamespace(
        media_type="application/pdf",
        file_name="manual.pdf",
        document_type="manual",
        source_system="EAM",
        parser_profile="pdf_deepdoc_v1",
        parser_profile_version="1",
    )
    routing = route_document_for_mapping(doc)
    assert routing["selected_parser_profile"] == "pdf_deepdoc_v1"
    assert routing["parser_version"] == "1"
    assert "PERSISTED_PROFILE_VERSION" in routing["routing_reasons"]


def test_persisted_profile_wins_over_changed_classification():
    doc = SimpleNamespace(
        media_type="text/csv",
        file_name="manual.csv",
        document_type="table",
        source_system="EAM",
        parser_profile="pdf_deepdoc_v1",
        parser_profile_version="1",
    )
    routing = route_document_for_mapping(doc)
    assert routing["selected_parser_profile"] == "pdf_deepdoc_v1"
    assert routing["parser_version"] == "1"
    assert routing["chunk_method"] == "naive"


def test_non_file_readiness_keeps_legacy_query_compatibility():
    doc = SimpleNamespace(
        source_kind="S3",
        current_version=1,
        business_status="active",
        sync_status="ready",
        pipeline_status="DONE",
        event_status="completed",
        source_state="AVAILABLE",
        ragflow_dataset_id="dataset-1",
        ragflow_document_id="document-1",
    )
    readiness = document_candidate_readiness(
        doc, quality_allowed=True, quality_required=False,
    )
    assert readiness.retrievable is True
    assert readiness.parser_readback is True


@pytest.mark.asyncio
async def test_schema_idempotent_and_version_isolated(tmp_path):
    db = await init_db(str(tmp_path / "schema.db"))
    await quality_models.ensure_quality_schema(db)
    routing = route_document(media_type="application/pdf", file_name="a.pdf")
    e1 = await quality_models.get_or_create_evaluation(
        db, "tenant-a", "EAM", "DOC-1", "v1",
        "ds-1", "doc-1", routing,
    )
    e2 = await quality_models.get_or_create_evaluation(
        db, "tenant-a", "EAM", "DOC-1", "v1",
        "ds-1", "doc-1", routing,
    )
    assert e1.id == e2.id
    e_other = await quality_models.get_or_create_evaluation(
        db, "tenant-b", "EAM", "DOC-1", "v2",
        "ds-2", "doc-2", routing,
    )
    latest_a = await quality_models.get_latest_evaluation(
        db, "tenant-a", "EAM", "DOC-1", "v1",
    )
    latest_b = await quality_models.get_latest_evaluation(
        db, "tenant-b", "EAM", "DOC-1", "v2",
    )
    assert latest_a.id == e1.id
    assert latest_b.id == e_other.id
    await db.close()


def test_quality_gate_fail_closed():
    assert enforce_quality_gate(None) == (False, "DOCUMENT_QUALITY_PENDING")
    pending = SimpleNamespace(evaluation_state="running", parse_quality_status=None)
    assert enforce_quality_gate(pending) == (False, "DOCUMENT_QUALITY_PENDING")
    review = SimpleNamespace(
        evaluation_state="completed", parse_quality_status="review_required"
    )
    assert enforce_quality_gate(review) == (False, "DOCUMENT_REVIEW_REQUIRED")
    failed = SimpleNamespace(
        evaluation_state="completed", parse_quality_status="failed"
    )
    assert enforce_quality_gate(failed) == (False, "DOCUMENT_QUALITY_FAILED")
    passed_without_evidence = SimpleNamespace(
        evaluation_state="completed", parse_quality_status="passed"
    )
    assert enforce_quality_gate(passed_without_evidence) == (
        False,
        "DOCUMENT_REVIEW_REQUIRED",
    )
    passed = SimpleNamespace(
        evaluation_state="completed", parse_quality_status="passed",
        metrics_json={
            "parse_success": True,
            "chunk_count": 1,
            "effective_text_coverage": 1.0,
            "garbled_char_ratio": 0.0,
            "position_coverage": 1.0,
            "required_capabilities": ["text", "position"],
            "quality_expectations": {"declarations_complete": True},
            "parserApplication": {
                "state": "ragflow_owned",
                "readbackMatch": True,
            },
        },
    )
    assert enforce_quality_gate(passed) == (True, None)
    # Dataset-owned parser differences are no longer a Gateway hard gate.
    parser_mismatch = SimpleNamespace(
        evaluation_state="completed",
        parse_quality_status="passed",
        metrics_json={
            **passed.metrics_json,
            "parserApplication": {
                "state": "mismatch",
                "readbackMatch": False,
            },
        },
    )
    assert enforce_quality_gate(parser_mismatch) == (True, None)
    assert enforce_quality_gate(
        passed_without_evidence,
        strict_mode=False,
        demo_warn_mode=True,
    ) == (True, "DOCUMENT_QUALITY_WARN")
    assert enforce_quality_gate(review, strict_mode=False, demo_warn_mode=True) == (
        True,
        "DOCUMENT_QUALITY_WARN",
    )


def test_quality_metrics_snapshot():
    metrics.reset()
    metrics.inc("quality_evaluation_pending_total")
    metrics.inc("quality_evaluation_passed_total")
    metrics.inc("quality_evaluation_review_required_total")
    metrics.inc("quality_evaluation_failed_total")
    metrics.observe_duration("quality_evaluation_duration", 1.25)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["quality_evaluation_pending_total"] == 1
    assert snapshot["counters"]["quality_evaluation_passed_total"] == 1
    assert snapshot["counters"]["quality_evaluation_review_required_total"] == 1
    assert snapshot["counters"]["quality_evaluation_failed_total"] == 1
    assert snapshot["duration_samples"] == 1
    metrics.reset()


class PassStub(RAGFlowDocumentStub):
    async def upload_document(self, dataset_id, file_name, file_content, request_id=None):
        result = await super().upload_document(
            dataset_id, file_name, file_content, request_id
        )
        result["data"][0]["chunk_count"] = 1
        return result

    async def list_chunks(
        self, dataset_id, document_id, page=1, page_size=30, request_id=None,
    ):
        return {
            "code": 0,
            "data": {
                "total": 1,
                "chunks": [
                    {
                        "id": "chunk-1",
                        "content": "设备 EQ-001：故障码 E-104 时先检查液压油位。",
                        "document_id": document_id,
                        "positions": [[1, 0.1, 0.1, 0.9, 0.9]],
                    }
                ],
            },
        }


class FlakyStub(PassStub):
    def __init__(self, fail_remaining: int = 1) -> None:
        super().__init__()
        self.fail_remaining = fail_remaining
        self._post_parse_reads = 0

    async def list_documents(
        self, dataset_id, document_id=None, page=1, page_size=100, request_id=None,
    ):
        if document_id and self._parse_calls:
            self._post_parse_reads += 1
            if self._post_parse_reads > 1 and self.fail_remaining:
                self.fail_remaining -= 1
                raise RAGFlowAPIError("Stub: RAGFlow unavailable", 503)
        return await super().list_documents(
            dataset_id, document_id, page, page_size, request_id
        )


class FailingQueryStub(RAGFlowQueryStub):
    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        raise RAGFlowAPIError("Stub: RAGFlow unavailable", 503)


async def _ready_with_client(db, client, content=b"manual"):
    service = SyncService(db, SourceStub(content), client)
    event = _make_event(content)
    doc, _ = await service.process_event(event)
    assert doc.sync_status == "ready"
    return doc


@pytest.mark.asyncio
async def test_worker_completes_passed_with_chunks():
    db = await init_db(":memory:")
    client = PassStub()
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    assert evaluation.evaluation_state == "completed"
    assert evaluation.parse_quality_status == "passed"
    assert evaluation.metrics_json["parserApplication"]["state"] == "ragflow_owned"
    assert evaluation.parse_repeatability_hash
    assert evaluation.e2e_repeatability_hash
    await db.close()


@pytest.mark.asyncio
async def test_worker_ignores_legacy_parser_mismatch_fields():
    db = await init_db(":memory:")
    client = PassStub()
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    await update_parser_application(db, doc, status="mismatch")
    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    assert evaluation.parse_quality_status == "passed"
    assert evaluation.metrics_json["parserApplication"]["state"] == "ragflow_owned"
    await db.close()


@pytest.mark.asyncio
async def test_worker_ignores_dataset_parser_config_differences():
    db = await init_db(":memory:")
    client = PassStub()
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    client._documents[doc.ragflow_document_id]["data"][0]["parser_config"][
        "layout_recognize"
    ] = "Plain Text"

    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    assert evaluation.parse_quality_status == "passed"
    assert "PARSER_APPLICATION_READBACK_MISMATCH" not in (
        evaluation.quality_reasons or []
    )
    await db.close()


class EmptyAfterReadyStub(PassStub):
    def __init__(self):
        super().__init__()
        self.empty_chunks = False

    async def list_chunks(
        self,
        dataset_id,
        document_id,
        page=1,
        page_size=30,
        request_id=None,
    ):
        if self.empty_chunks:
            return {"code": 0, "data": {"total": 0, "chunks": [], "doc": {}}}
        return await super().list_chunks(
            dataset_id, document_id, page, page_size, request_id
        )


@pytest.mark.asyncio
async def test_worker_empty_chunks_is_not_passed():
    db = await init_db(":memory:")
    client = EmptyAfterReadyStub()
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    client.empty_chunks = True
    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    assert evaluation.evaluation_state == "completed"
    assert evaluation.parse_quality_status != "passed"
    await db.close()


@pytest.mark.asyncio
async def test_worker_retries_then_recovers():
    db = await init_db(":memory:")
    client = FlakyStub(fail_remaining=0)
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    client.fail_remaining = 1
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    await db.execute(
        "UPDATE quality_evaluation_job SET max_attempts=2 WHERE evaluation_id=?",
        (evaluation.id,),
    )
    await db.commit()
    quality = QualityEvaluationService(db, client)
    worker = QualityEvaluationWorker(quality)
    await worker.run_once()
    job = await quality_models.get_job_by_evaluation_id(db, evaluation.id)
    assert job.status == "pending"
    await db.execute(
        "UPDATE quality_evaluation_job SET next_retry_at=NULL WHERE id=?",
        (job.id,),
    )
    await db.commit()
    await worker.run_once()
    evaluation = await quality_models.get_evaluation_by_id(db, evaluation.id)
    job = await quality_models.get_job_by_evaluation_id(db, evaluation.id)
    assert evaluation.evaluation_state == "completed"
    assert evaluation.parse_quality_status == "passed"
    assert job.status == "done"
    await db.close()


@pytest.mark.asyncio
async def test_worker_dead_letter_never_passes():
    db = await init_db(":memory:")
    client = FlakyStub(fail_remaining=0)
    client.run_status = "DONE"
    doc = await _ready_with_client(db, client)
    client.fail_remaining = 99
    evaluation = await quality_models.get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    await db.execute(
        "UPDATE quality_evaluation_job SET max_attempts=1 WHERE evaluation_id=?",
        (evaluation.id,),
    )
    await db.commit()
    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_evaluation_by_id(db, evaluation.id)
    job = await quality_models.get_job_by_evaluation_id(db, evaluation.id)
    assert evaluation.parse_quality_status != "passed"
    assert evaluation.evaluation_state == "failed"
    assert job.status == "dead"
    await db.close()


@pytest.mark.asyncio
async def test_worker_marks_failed_document_failed():
    db = await init_db(":memory:")
    doc = ExtDocumentMap(
        tenant_id="customer-a",
        source_system="DEMO",
        external_document_id="DOC-FAIL",
        source_version_id="v1",
        event_id=str(uuid.uuid4()),
        sha256=hashlib.sha256(b"broken").hexdigest(),
        file_name="broken.pdf",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-fail",
        source_page_count=1,
        sync_status="failed",
    )
    doc = await insert_mapping(db, doc)
    await update_mapping_status(db, doc, "failed", pipeline_status="FAIL")
    routing = route_document(media_type=doc.media_type, file_name=doc.file_name)
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=doc.ragflow_dataset_id,
        ragflow_document_id=doc.ragflow_document_id,
        routing=routing,
        evaluation_version="1",
    )
    client = RAGFlowDocumentStub()
    client.run_status = "FAIL"
    client._documents["doc-fail"] = {
        "data": [
            {
                "id": "doc-fail",
                "dataset_id": "ds-1",
                "run": "FAIL",
                "page_count": 1,
            }
        ]
    }
    quality = QualityEvaluationService(db, client)
    await QualityEvaluationWorker(quality).run_once()
    evaluation = await quality_models.get_evaluation_by_id(db, evaluation.id)
    assert evaluation.evaluation_state == "completed"
    assert evaluation.parse_quality_status == "failed"
    assert "RAGFLOW_PARSE_FAILED" in evaluation.quality_reasons
    await db.close()


@pytest.mark.usefixtures("isolated_phase2_db")
class TestQualityAPI:
    @pytest.mark.asyncio
    async def test_quality_api_requires_token(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/documents/DOC-NOPE/quality",
                params={"source_system": "DEMO"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_quality_api_missing_document_returns_404(self):
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/documents/DOC-MISSING/quality",
                params={"source_system": "DEMO"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "DOCUMENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_reevaluate_requires_params(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/documents/DOC-A/quality:reevaluate",
            )
            assert resp.status_code == 422
            assert resp.json()["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_quality_api_authorized_and_reevaluate(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        doc = await _insert_ready_document(db, doc_id="DOC-A")
        await _create_evaluation(db, doc, quality_status="passed")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/documents/DOC-A/quality",
                params={"source_system": "DEMO"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["parseQualityStatus"] == "passed"
            assert body["evaluationState"] == "completed"

            list_resp = await c.get(
                "/enterprise/api/v1/documents/quality-status",
                params={"status": "passed"},
            )
            assert list_resp.status_code == 200
            assert len(list_resp.json()) == 1

            re_resp = await c.post(
                "/enterprise/api/v1/documents/DOC-A/quality:reevaluate",
                params={
                    "tenant_id": "customer-a",
                    "source_system": "DEMO",
                    "source_version_id": "v1",
                },
            )
            assert re_resp.status_code == 202
            assert re_resp.json()["evaluationVersion"] == "2"
            latest = await quality_models.get_latest_evaluation(
                db, "customer-a", "DEMO", "DOC-A", "v1",
            )
            assert latest.evaluation_version == "2"

    @pytest.mark.asyncio
    async def test_quality_api_uses_formal_document_acl(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        doc = await _insert_ready_document(
            db, doc_id="DOC-FORMAL", allowed_users=()
        )
        await _create_evaluation(db, doc, quality_status="passed")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/documents/DOC-FORMAL/quality",
                params={"source_system": "DEMO"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert resp.json()["parseQualityStatus"] == "passed"

    @pytest.mark.asyncio
    async def test_role_group_mismatch_is_open_during_test_stage(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        await _insert_ready_document(
            db, doc_id="DOC-B", allow_groups=("other-group",)
        )
        token_b = _make_token(user="biz-user-002")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/documents/DOC-B/quality",
                params={"source_system": "DEMO"},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("code") != "ACL_DENIED"
            # No evaluation row yet — ACL open still allows the endpoint.
            assert body.get("evaluationState") in {None, "not_started"}

    @pytest.mark.asyncio
    async def test_ask_gate_blocks_review_required(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        doc = await _insert_ready_document(db, doc_id="DOC-C")
        await _create_evaluation(
            db, doc, quality_status="review_required",
            reasons=["PAGE_COVERAGE_BELOW_MIN"],
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-C",
                    "question": "检查步骤是什么",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 409
            assert resp.json()["code"] == "DOCUMENT_REVIEW_REQUIRED"

    @pytest.mark.asyncio
    async def test_ask_gate_allows_passed(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        doc = await _insert_ready_document(db, doc_id="DOC-D")
        await _create_evaluation(db, doc, quality_status="passed")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-D",
                    "question": "检查步骤是什么",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert resp.json()["answer"]


    @pytest.mark.asyncio
    async def test_ask_ragflow_unavailable_returns_503(self):
        from enterprise.gateway.quality import router as quality_router_module

        db = app.dependency_overrides[quality_router_module.get_db]()
        doc = await _insert_ready_document(db, doc_id="DOC-503")
        await _create_evaluation(db, doc, quality_status="passed")
        query_router._query_stub = FailingQueryStub()
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={"externalDocumentId": "DOC-503", "question": "检查步骤"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 503
            assert resp.json()["code"] == "RAGFLOW_UNAVAILABLE"


@pytest.mark.asyncio
async def test_backfill_dry_run_then_real(tmp_path):
    db_path = str(tmp_path / "backfill.db")
    db = await init_db(db_path)
    await _insert_ready_document(db, doc_id="DOC-E")
    args = argparse.Namespace(
        db=db_path,
        tenant="customer-a",
        source_system="DEMO",
        source_version_id="v1",
        dry_run=True,
        limit=100,
        offset=0,
        max_attempts=5,
    )
    await backfill_run(args)
    latest = await quality_models.get_latest_evaluation(
        db, "customer-a", "DEMO", "DOC-E", "v1",
    )
    assert latest is None
    args.dry_run = False
    await backfill_run(args)
    latest = await quality_models.get_latest_evaluation(
        db, "customer-a", "DEMO", "DOC-E", "v1",
    )
    assert latest is not None
    assert latest.evaluation_state == "pending"
    await db.close()


@pytest.mark.asyncio
async def test_backfill_skips_disabled_and_is_idempotent(tmp_path):
    db_path = str(tmp_path / "backfill2.db")
    db = await init_db(db_path)
    doc = await _insert_ready_document(db, doc_id="DOC-DISABLED")
    await update_mapping_status(
        db, doc, "disabled", business_status="disabled",
    )
    args = argparse.Namespace(
        db=db_path,
        tenant="customer-a",
        source_system="DEMO",
        source_version_id="v1",
        dry_run=False,
        limit=100,
        offset=0,
        max_attempts=5,
    )
    await backfill_run(args)
    latest = await quality_models.get_latest_evaluation(
        db, "customer-a", "DEMO", "DOC-DISABLED", "v1",
    )
    assert latest is None
    await db.close()


@pytest.mark.asyncio
async def test_backfill_resume_and_idempotent(tmp_path):
    db_path = str(tmp_path / "backfill3.db")
    db = await init_db(db_path)
    for index in range(3):
        await _insert_ready_document(db, doc_id=f"DOC-RESUME-{index}")
    base = argparse.Namespace(
        db=db_path,
        tenant="customer-a",
        source_system="DEMO",
        source_version_id="v1",
        dry_run=False,
        limit=2,
        offset=0,
        max_attempts=5,
    )
    await backfill_run(base)
    first = await quality_models.list_evaluations(
        db, tenant_id="customer-a", source_system="DEMO",
    )
    assert len(first) == 2
    base.offset = 2
    await backfill_run(base)
    second = await quality_models.list_evaluations(
        db, tenant_id="customer-a", source_system="DEMO",
    )
    assert len(second) == 3
    await backfill_run(base)
    third = await quality_models.list_evaluations(
        db, tenant_id="customer-a", source_system="DEMO",
    )
    assert len(third) == 3
    await db.close()


@pytest.mark.asyncio
async def test_quality_reconciler_fails_stuck_running():
    from datetime import datetime, timedelta, timezone

    db = await init_db(":memory:")
    doc = await _insert_ready_document(db, doc_id="DOC-STUCK")
    routing = route_document(media_type=doc.media_type, file_name=doc.file_name)
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=doc.ragflow_dataset_id,
        ragflow_document_id=doc.ragflow_document_id,
        routing=routing,
        evaluation_version="1",
    )
    locked_at = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    await db.execute(
        """UPDATE quality_evaluation_job
           SET status='running', locked_at=?, worker_id='stuck-worker'
           WHERE evaluation_id=?""",
        (locked_at, evaluation.id),
    )
    await db.commit()
    service = QualityEvaluationService(db, RAGFlowDocumentStub())
    reconciler = QualityReconciler(service, running_timeout_seconds=60)
    await reconciler.run_once()
    evaluation = await quality_models.get_evaluation_by_id(db, evaluation.id)
    assert evaluation.evaluation_state == "failed"
    assert evaluation.last_error_code == "QUALITY_RUNNING_TIMEOUT"
    await db.close()
