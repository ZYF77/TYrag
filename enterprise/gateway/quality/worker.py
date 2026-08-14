"""Asynchronous quality evaluation worker and reconciler."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from enterprise.gateway.quality.gate import (
    quality_dimensions,
    required_quality_dimensions,
)
from enterprise.gateway.quality.metrics import metrics
from enterprise.gateway.quality.models import (
    QualityJob,
    claim_quality_job,
    complete_evaluation,
    fail_evaluation,
    get_evaluation_by_id,
    get_or_create_evaluation,
    mark_quality_job_done,
    mark_quality_job_failed,
    mark_quality_job_retry,
    row_to_job,
    start_evaluation,
    utc_now,
)
from enterprise.gateway.quality.routing import (
    parser_application_readback_match,
    parser_configuration_matches,
    route_document_for_mapping,
)
from enterprise.gateway.sync.models import get_mapping, list_mappings
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError
from enterprise.gateway.sync.sync_service import promote_quality_passed_version
from enterprise.scripts.wp03.metrics import (
    compute_document_metrics,
    e2e_repeatability_hash,
    parse_repeatability_hash,
)
from enterprise.scripts.wp03.quality_gate import evaluate_document_quality, load_thresholds

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
TERMINAL_RUNS = {"DONE", "3", "FAIL", "4", "CANCEL", "2"}


def _git_state(root: Path = ROOT) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def _version_manifest() -> dict[str, Any]:
    path = ROOT / "version-manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _decoded_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _quality_meta(doc_info: dict[str, Any]) -> dict[str, Any]:
    value = _decoded_json(doc_info.get("meta_fields") or {})
    return value if isinstance(value, dict) else {}


def _parser_application_snapshot(doc: Any) -> dict[str, Any]:
    def profile(raw: Any) -> str | None:
        value = _decoded_json(raw)
        return value.get("profile") if isinstance(value, dict) else None

    state = getattr(doc, "parser_application_status", None) or "legacy_unverified"
    readback_match = parser_application_readback_match(doc)
    reason_code = None
    if not readback_match:
        reason_code = (
            f"PARSER_APPLICATION_{state.upper()}"
            if state != "executed"
            else "PARSER_APPLICATION_READBACK_MISMATCH"
        )
    return {
        "state": state,
        "selectedProfile": getattr(doc, "parser_profile", None),
        "configuredProfile": profile(getattr(doc, "parser_configured_json", None)),
        "executedProfile": profile(getattr(doc, "parser_executed_json", None)),
        "readbackMatch": readback_match,
        "reasonCode": reason_code,
    }


def _meta_entry(meta: dict[str, Any], name: str) -> tuple[Any, bool]:
    for key in (f"enterprise_quality_{name}", f"quality_{name}"):
        if key in meta:
            return meta[key], True
    return None, False


def _quality_expectations(
    doc: Any,
    doc_info: dict[str, Any],
) -> tuple[
    list[int],
    dict[str, str],
    bool,
    list[dict[str, Any]],
    list[str],
    dict[str, Any],
]:
    """Read fail-closed quality expectations from persisted/public metadata.

    Asset identifiers persisted by the enterprise mapping describe business
    scope; they are not proof that an OCR scan must contain those identifiers.
    Only an explicit namespaced RAGFlow ``meta_fields`` declaration is used as
    parse ground truth.
    """
    meta = _quality_meta(doc_info)

    expected_tables_value, expected_tables_declared = _meta_entry(
        meta, "expected_tables"
    )
    expected_tables_raw = _decoded_json(expected_tables_value)
    expected_tables: list[int] = []
    if isinstance(expected_tables_raw, list):
        for value in expected_tables_raw:
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page >= 1 and page not in expected_tables:
                expected_tables.append(page)

    ground_truth_fields: dict[str, str] = {}
    metadata_fields_value, metadata_fields_declared = _meta_entry(
        meta, "ground_truth_fields"
    )
    if not metadata_fields_declared:
        metadata_fields_value, metadata_fields_declared = _meta_entry(
            meta, "ground_truth_json"
        )
    metadata_fields = _decoded_json(metadata_fields_value)
    if isinstance(metadata_fields, dict):
        for field, value in metadata_fields.items():
            if value is not None and str(value).strip():
                ground_truth_fields[str(field)] = str(value)

    citation_expected_value, citation_expected_declared = _meta_entry(
        meta, "citation_expected"
    )
    citation_expected_raw = _decoded_json(citation_expected_value)
    citation_expected = (
        citation_expected_raw is True
        or str(citation_expected_raw or "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    citation_results_value, _ = _meta_entry(meta, "citation_results")
    citation_results_raw = _decoded_json(citation_results_value)
    citation_results = (
        [item for item in citation_results_raw if isinstance(item, dict)]
        if isinstance(citation_results_raw, list)
        else []
    )

    required_value, required_declared = _meta_entry(
        meta, "required_capabilities"
    )
    required_raw = _decoded_json(required_value)
    required = ["text", "position"]
    if isinstance(required_raw, list):
        for value in required_raw:
            name = str(value or "").strip().lower()
            if name and name not in required:
                required.append(name)
    if expected_tables and "table" not in required:
        required.append("table")
    if ground_truth_fields and "key_field" not in required:
        required.append("key_field")
    if citation_expected and "citation" not in required:
        required.append("citation")

    ground_truth_declared = metadata_fields_declared or bool(
        ground_truth_fields
    )
    declaration_status = {
        "expected_tables": expected_tables_declared,
        "ground_truth_fields": ground_truth_declared,
        "citation_expected": citation_expected_declared,
        "required_capabilities": required_declared
        and isinstance(required_raw, list)
        and bool(required_raw),
    }
    missing_declarations = sorted(
        name for name, declared in declaration_status.items() if not declared
    )
    expectation_summary = {
        "expected_table_pages": expected_tables,
        "ground_truth_fields": sorted(ground_truth_fields),
        "citation_expected": citation_expected,
        "declarations": declaration_status,
        "missing_declarations": missing_declarations,
        "declarations_complete": not missing_declarations,
    }
    return (
        expected_tables,
        ground_truth_fields,
        citation_expected,
        citation_results,
        required,
        expectation_summary,
    )


class QualityRetryableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class QualityEvaluationService:
    def __init__(
        self,
        db: aiosqlite.Connection,
        ragflow_client,
        thresholds_path: str | Path | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.db = db
        self.ragflow_client = ragflow_client
        self.thresholds_path = thresholds_path or (
            ROOT / "enterprise" / "scripts" / "wp03" / "thresholds.json"
        )
        self.max_attempts = max_attempts

    async def ensure_quality_evaluation(self, doc) -> Any | None:
        """Create an idempotent pending evaluation for a ready document."""
        if (
            doc.sync_status != "ready"
            or doc.business_status != "active"
            or not doc.ragflow_dataset_id
            or not doc.ragflow_document_id
        ):
            return None
        routing = route_document_for_mapping(doc)
        return await get_or_create_evaluation(
            self.db,
            tenant_id=doc.tenant_id,
            source_system=doc.source_system,
            external_document_id=doc.external_document_id,
            source_version_id=doc.source_version_id,
            ragflow_dataset_id=doc.ragflow_dataset_id,
            ragflow_document_id=doc.ragflow_document_id,
            routing=routing,
            evaluation_version="1",
            max_attempts=self.max_attempts,
        )

    async def run_job(self, job: QualityJob) -> None:
        started = time.monotonic()
        evaluation = await get_evaluation_by_id(self.db, job.evaluation_id)
        if evaluation is None:
            await mark_quality_job_failed(
                self.db, job, "QUALITY_EVALUATION_NOT_FOUND", "Evaluation row missing"
            )
            return
        doc = await get_mapping(
            self.db,
            job.tenant_id,
            job.source_system,
            job.external_document_id,
            job.source_version_id,
        )
        if doc is None:
            await fail_evaluation(
                self.db, evaluation.id, "DOCUMENT_NOT_FOUND", "Document mapping missing"
            )
            await mark_quality_job_failed(
                self.db, job, "DOCUMENT_NOT_FOUND", "Document mapping missing"
            )
            return
        if (
            doc.sync_status not in ("ready", "failed")
            or doc.business_status in ("disabled", "deleted", "superseded")
        ):
            await fail_evaluation(
                self.db,
                evaluation.id,
                "DOCUMENT_NOT_READY_FOR_QUALITY",
                f"sync_status={doc.sync_status} business_status={doc.business_status}",
            )
            await mark_quality_job_failed(
                self.db,
                job,
                "DOCUMENT_NOT_READY_FOR_QUALITY",
                "Document is not ready and active",
            )
            return

        await start_evaluation(self.db, evaluation.id)
        metrics.inc("quality_evaluation_running_total")
        try:
            result = await self._evaluate(doc, evaluation)
            await self._complete(doc, evaluation, result)
            if result["parse_quality_status"] == "passed":
                try:
                    await promote_quality_passed_version(
                        self.db,
                        self.ragflow_client,
                        doc,
                        result["parse_quality_status"],
                    )
                except RAGFlowAPIError as exc:
                    raise QualityRetryableError(
                        "RAGFLOW_UNAVAILABLE",
                        "Quality passed but version promotion could not complete",
                    ) from exc
            status_metric = {
                "passed": "quality_evaluation_passed_total",
                "review_required": "quality_evaluation_review_required_total",
                "failed": "quality_evaluation_failed_total",
            }.get(result["parse_quality_status"])
            if status_metric:
                metrics.inc(status_metric)
            metrics.observe_duration(
                "quality_evaluation_duration", time.monotonic() - started
            )
            logger.info(
                "quality evaluation completed evaluation_id=%s tenant_id=%s "
                "external_document_id=%s source_version_id=%s "
                "ragflow_document_id=%s parser_profile=%s quality_status=%s "
                "quality_reasons=%s attempt=%s duration_seconds=%.3f",
                evaluation.id,
                doc.tenant_id,
                doc.external_document_id,
                doc.source_version_id,
                doc.ragflow_document_id,
                evaluation.parser_profile,
                result["parse_quality_status"],
                result["quality_reasons"],
                job.attempts,
                time.monotonic() - started,
            )
            await mark_quality_job_done(self.db, job)
            await self._emit_terminal_callback(doc, result)
        except QualityRetryableError as exc:
            metrics.inc("quality_evaluation_retry_total")
            logger.warning(
                "quality evaluation retry evaluation_id=%s tenant_id=%s "
                "external_document_id=%s source_version_id=%s attempt=%s "
                "error_code=%s",
                evaluation.id,
                doc.tenant_id,
                doc.external_document_id,
                doc.source_version_id,
                job.attempts,
                exc.code,
            )
            await fail_evaluation(
                self.db, evaluation.id, exc.code, exc.message,
            )
            if job.attempts < job.max_attempts:
                await mark_quality_job_retry(self.db, job, exc.code, exc.message)
            else:
                metrics.inc("quality_evaluation_failed_total")
                await mark_quality_job_failed(self.db, job, exc.code, exc.message)
                await self._emit_terminal_failed(
                    doc,
                    code=exc.code,
                    message=exc.message,
                )
        except Exception:
            logger.exception(
                "Quality evaluation failed evaluation_id=%s job_id=%s",
                evaluation.id,
                job.id,
            )
            await fail_evaluation(
                self.db, evaluation.id, "INTERNAL_ERROR", "Quality evaluation failed"
            )
            metrics.inc("quality_evaluation_failed_total")
            await mark_quality_job_failed(
                self.db, job, "INTERNAL_ERROR", "Quality evaluation failed"
            )
            await self._emit_terminal_failed(
                doc,
                code="INTERNAL_ERROR",
                message="Quality evaluation failed",
            )

    async def _evaluate(self, doc, evaluation) -> dict[str, Any]:
        started = time.monotonic()
        if not doc.ragflow_dataset_id or not doc.ragflow_document_id:
            return self._failed_result(
                doc, "RAGFLOW_DOCUMENT_MISSING", "RAGFlow document id missing"
            )
        try:
            docs = await self.ragflow_client.list_documents(
                doc.ragflow_dataset_id,
                document_id=doc.ragflow_document_id,
            )
        except RAGFlowAPIError as exc:
            raise QualityRetryableError("RAGFLOW_UNAVAILABLE", str(exc)) from exc
        if not docs:
            return self._failed_result(
                doc, "RAGFLOW_DOCUMENT_MISSING",
                "RAGFlow document not found during evaluation",
            )
        doc_info = docs[0]
        run = str(doc_info.get("run") or "").upper()
        if run not in TERMINAL_RUNS:
            raise QualityRetryableError(
                "RAGFLOW_NOT_TERMINAL", f"RAGFlow run status is {run}"
            )
        try:
            routing = route_document_for_mapping(doc)
        except ValueError as exc:
            return self._failed_result(
                doc, "PARSER_PROFILE_UNVERIFIED", str(exc),
            )
        parser_readback_matches = parser_configuration_matches(routing, doc_info)

        chunks: list[dict[str, Any]] = []
        page = 1
        page_size = 100
        while True:
            try:
                result = await self.ragflow_client.list_chunks(
                    doc.ragflow_dataset_id,
                    doc.ragflow_document_id,
                    page=page,
                    page_size=page_size,
                )
            except RAGFlowAPIError as exc:
                raise QualityRetryableError("RAGFLOW_UNAVAILABLE", str(exc)) from exc
            data = result.get("data") or {}
            batch = data.get("chunks") or []
            chunks.extend(batch)
            total = int(data.get("total") or 0)
            if not batch or len(chunks) >= total or len(batch) < page_size:
                break
            page += 1

        normalized = [
            {
                "id": chunk.get("id"),
                "content": chunk.get("content", ""),
                "document_id": chunk.get("document_id"),
                "dataset_id": chunk.get("dataset_id"),
                "positions": chunk.get("positions") or [],
                "image_id": chunk.get("image_id"),
                "doc_type_kwd": chunk.get("doc_type_kwd"),
                "available": chunk.get("available"),
            }
            for chunk in chunks
        ]
        try:
            source_page_count = int(
                doc.source_page_count
                or doc_info.get("page_count")
                or doc_info.get("page_num")
                or doc_info.get("total_pages")
                or 0
            )
        except (TypeError, ValueError):
            source_page_count = 0

        (
            expected_tables,
            ground_truth_fields,
            citation_expected,
            citation_results,
            required_capabilities,
            expectation_summary,
        ) = _quality_expectations(doc, doc_info)

        metrics = compute_document_metrics(
            doc_info,
            normalized,
            source_page_count,
            ground_truth_fields=ground_truth_fields or None,
            expected_tables=expected_tables or None,
            wall_clock_duration_seconds=round(time.monotonic() - started, 3),
            citation_results=citation_results or None,
        )
        metrics["file_sha256"] = doc.sha256
        metrics["required_capabilities"] = required_capabilities
        metrics["quality_expectations"] = expectation_summary
        thresholds = load_thresholds(self.thresholds_path)
        quality_status, reasons = evaluate_document_quality(
            metrics,
            thresholds,
            expected_tables=expected_tables or None,
            ground_truth_fields=ground_truth_fields or None,
            citation_expected=citation_expected,
        )
        dimensions = quality_dimensions(metrics)
        metrics["quality_dimensions"] = dimensions
        parser_application = _parser_application_snapshot(doc)
        if not parser_readback_matches:
            parser_application["readbackMatch"] = False
            parser_application["reasonCode"] = "PARSER_APPLICATION_READBACK_MISMATCH"
        metrics["parserApplication"] = parser_application
        if quality_status == "passed" and not (
            parser_application["state"] == "executed"
            and parser_application["readbackMatch"] is True
        ):
            quality_status = "review_required"
            parser_reason = (
                "PARSER_APPLICATION_NOT_EXECUTED"
                if parser_application["state"] != "executed"
                else "PARSER_APPLICATION_READBACK_MISMATCH"
            )
            if parser_reason not in reasons:
                reasons.append(parser_reason)
        required_dimensions, declaration_valid = required_quality_dimensions(metrics)
        if not declaration_valid:
            if quality_status == "passed":
                quality_status = "review_required"
            if "REQUIRED_CAPABILITY_INVALID" not in reasons:
                reasons.append("REQUIRED_CAPABILITY_INVALID")
        missing_required = [
            name for name in required_dimensions
            if dimensions.get(name) != "passed"
        ]
        if missing_required:
            if quality_status == "passed":
                quality_status = "review_required"
            if "REQUIRED_CAPABILITY_NOT_PASSED" not in reasons:
                reasons.append("REQUIRED_CAPABILITY_NOT_PASSED")
        metrics["required_quality_dimensions"] = list(required_dimensions)
        metrics["missing_required_quality_dimensions"] = missing_required
        metrics["quality_status"] = quality_status
        return {
            "sample_id": f"{doc.external_document_id}:{doc.source_version_id}",
            "sync_status": doc.sync_status,
            "parse_quality_status": quality_status,
            "quality_reasons": reasons,
            "metrics": metrics,
            "chunks": normalized,
        }

    def _failed_result(self, doc, code: str, message: str) -> dict[str, Any]:
        metrics = {
            "document_id": None,
            "dataset_id": doc.ragflow_dataset_id,
            "file_sha256": doc.sha256,
            "parsing_status": "FAIL",
            "error_code": code,
            "parse_success": False,
            "chunk_count": 0,
            "page_count_source": doc.source_page_count or 0,
            "page_count_observed": 0,
            "quality_status": "failed",
            "quality_dimensions": {
                "text_quality": "failed",
                "table_quality": "failed",
                "position_quality": "failed",
                "key_field_quality": "failed",
                "citation_quality": "failed",
                "image_semantic_quality": "failed",
            },
        }
        return {
            "sample_id": f"{doc.external_document_id}:{doc.source_version_id}",
            "sync_status": doc.sync_status,
            "parse_quality_status": "failed",
            "quality_reasons": [code, message],
            "metrics": metrics,
            "chunks": [],
        }

    async def _complete(self, doc, evaluation, result: dict[str, Any]) -> None:
        thresholds = load_thresholds(self.thresholds_path)
        commit, dirty = _git_state()
        manifest = _version_manifest().get("ragflow_upstream", {})
        parse_hash = parse_repeatability_hash([result])
        e2e_hash = e2e_repeatability_hash([result])
        artifact_hash = self._artifact_hash(evaluation.id, result)
        await complete_evaluation(
            self.db,
            evaluation.id,
            parse_quality_status=result["parse_quality_status"],
            quality_reasons=result["quality_reasons"],
            metrics_json=result["metrics"],
            parse_repeatability_hash=parse_hash,
            e2e_repeatability_hash=e2e_hash,
            artifact_hash=artifact_hash,
            enterprise_commit=commit,
            enterprise_worktree_dirty=dirty,
            ragflow_source_tag=manifest.get("source_tag"),
            ragflow_source_commit=manifest.get("source_commit"),
            thresholds_version=str(thresholds.get("threshold_version") or ""),
            thresholds_digest=self._thresholds_digest(),
        )

    def _thresholds_digest(self) -> str:
        path = Path(self.thresholds_path)
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _artifact_hash(evaluation_id: int, result: dict[str, Any]) -> str:
        stable = json.dumps(
            {
                "evaluation_id": evaluation_id,
                "completed_at": utc_now(),
                "metrics": result.get("metrics") or {},
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    async def _emit_terminal_callback(self, doc, result: dict[str, Any]) -> None:
        from enterprise.gateway.callback_delivery import emit_terminal_callback_safe
        from enterprise.gateway.sync.readiness import document_candidate_readiness_from_db

        quality_status = str(result.get("parse_quality_status") or "")
        fresh = await get_mapping(
            self.db,
            doc.tenant_id,
            doc.source_system,
            doc.external_document_id,
            doc.source_version_id,
        ) or doc
        if quality_status == "passed":
            readiness, _ = await document_candidate_readiness_from_db(self.db, fresh)
            if readiness.retrievable:
                await emit_terminal_callback_safe(
                    self.db,
                    fresh,
                    "retrievable",
                    quality_status="passed",
                    retrievable=True,
                )
            else:
                # Quality passed but version was not promoted; notify EAM.
                await emit_terminal_callback_safe(
                    self.db,
                    fresh,
                    "review_required",
                    quality_status="passed",
                    retrievable=False,
                )
            return
        if quality_status == "review_required":
            await emit_terminal_callback_safe(
                self.db,
                fresh,
                "review_required",
                quality_status="review_required",
                retrievable=False,
            )
            return
        if quality_status == "failed":
            await self._emit_terminal_failed(
                fresh,
                code="DOCUMENT_QUALITY_FAILED",
                message="Document quality evaluation failed",
            )

    async def _emit_terminal_failed(
        self,
        doc,
        *,
        code: str,
        message: str,
    ) -> None:
        from enterprise.gateway.callback_delivery import emit_terminal_callback_safe

        await emit_terminal_callback_safe(
            self.db,
            doc,
            "failed",
            quality_status="failed",
            retrievable=False,
            error={"code": code, "message": message, "retryable": False},
        )


class QualityEvaluationWorker:
    def __init__(
        self, service: QualityEvaluationService, worker_id: str | None = None,
    ) -> None:
        self.service = service
        self.worker_id = worker_id or f"quality-{uuid.uuid4().hex[:8]}"

    async def run_once(self, limit: int = 1) -> int:
        jobs = await claim_quality_job(self.service.db, self.worker_id, limit)
        for job in jobs:
            await self.service.run_job(job)
        return len(jobs)

    async def run_forever(self, interval_seconds: float = 2.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Quality evaluation worker iteration failed")
            await asyncio.sleep(interval_seconds)


class QualityReconciler:
    def __init__(
        self,
        service: QualityEvaluationService,
        running_timeout_seconds: int = 1800,
    ) -> None:
        self.service = service
        self.running_timeout_seconds = running_timeout_seconds

    async def run_once(self, limit: int = 100) -> int:
        created = 0
        docs = await list_mappings(
            self.service.db,
            status="ready",
            limit=limit,
            ascending=True,
        )
        for doc in docs:
            if doc.business_status == "active":
                await self.service.ensure_quality_evaluation(doc)
                created += 1

        await self._fail_stuck_running()
        return created

    async def _fail_stuck_running(self) -> None:
        from datetime import datetime, timedelta, timezone

        threshold = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.running_timeout_seconds)
        ).isoformat()
        async with self.service.db.execute(
            """SELECT * FROM quality_evaluation_job
               WHERE status='running' AND locked_at IS NOT NULL AND locked_at < ?""",
            (threshold,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            job = row_to_job(row)
            await fail_evaluation(
                self.service.db,
                job.evaluation_id,
                "QUALITY_RUNNING_TIMEOUT",
                "Quality evaluation exceeded running timeout",
            )
            await mark_quality_job_failed(
                self.service.db,
                job,
                "QUALITY_RUNNING_TIMEOUT",
                "Quality evaluation exceeded running timeout",
            )

    async def run_forever(self, interval_seconds: float = 10.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Quality reconciler iteration failed")
            await asyncio.sleep(interval_seconds)
