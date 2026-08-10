"""Formal WP-03 S1-S8 acceptance orchestrator.

The formal corpus is supplied out of band because it contains sanitized,
human-reviewed source documents.  Missing corpus files or live credentials are
reported as BLOCKED (exit 2), never as skipped tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SCENARIOS = {
    "S1": ("ocr", "key_fields", "citation"),
    "S2": ("ocr", "table_structure", "table_crop", "citation"),
    "S3": ("image_crop", "image_ocr", "image_caption", "citation"),
    "S4": ("diagram_labels", "diagram_relations", "citation"),
    "S5": ("flowchart_nodes", "flowchart_arrows", "flowchart_branches", "vlm", "citation"),
    "S6": ("native_text", "ocr", "page_coverage", "citation"),
    "S7a": ("orientation", "degraded_ocr", "quality_review", "citation"),
    "S7b": ("orientation", "degraded_ocr", "quality_failure", "citation"),
    "S8": ("multi_page_chunking", "metadata", "citation"),
}
SCENARIO_MINIMUMS = {
    "S1": 2,
    "S2": 2,
    "S3": 2,
    "S4": 2,
    "S5": 2,
    "S6": 2,
    "S7a": 1,
    "S7b": 1,
    "S8": 2,
}
SCENARIO_CATEGORIES = {
    "S1": "clear_scan",
    "S2": "table_dense",
    "S3": "mixed_manual",
    "S4": "diagram",
    "S5": "diagram",
    "S6": "mixed_manual",
    "S7a": "degraded_scan",
    "S7b": "degraded_scan",
    "S8": "digital_text",
}
SCENARIO_EXPECTED_QUALITY = {
    **{scenario_id: "passed" for scenario_id in SCENARIOS},
    "S7b": "review_required",
}
SCENARIO_REQUIRED_CAPABILITIES = {
    "S1": ("text", "position", "key_field", "citation"),
    "S2": ("text", "position", "table", "citation"),
    "S3": ("text", "position", "image_semantic", "citation"),
    "S4": ("position", "image_semantic", "citation"),
    "S5": ("position", "image_semantic", "citation"),
    "S6": ("text", "position", "citation"),
    "S7a": ("text", "position", "citation"),
    "S7b": ("text", "position", "citation"),
    "S8": ("text", "position", "key_field", "citation"),
}
_COMMON_FORMAL_METRICS = (
    "ocr_cer",
    "citation_bbox_iou",
    "retrieval_recall_at_8",
    "no_answer_refusal_accuracy",
    "citation_version_accuracy",
)
SCENARIO_REQUIRED_METRICS = {
    "S1": _COMMON_FORMAL_METRICS,
    "S2": _COMMON_FORMAL_METRICS + ("table_cell_accuracy",),
    "S3": _COMMON_FORMAL_METRICS + ("image_ocr_recall", "image_caption_recall"),
    "S4": _COMMON_FORMAL_METRICS + ("image_ocr_recall", "image_caption_recall"),
    "S5": _COMMON_FORMAL_METRICS + (
        "image_ocr_recall",
        "image_caption_recall",
        "flowchart_node_recall",
        "flowchart_edge_recall",
        "flowchart_condition_recall",
        "flowchart_warning_recall",
        "vlm_description_recall",
    ),
    "S6": _COMMON_FORMAL_METRICS,
    "S7a": _COMMON_FORMAL_METRICS,
    "S7b": _COMMON_FORMAL_METRICS,
    "S8": _COMMON_FORMAL_METRICS,
}
FORMAL_METRIC_THRESHOLDS = {
    "ocr_cer": ("max", 0.05),
    "citation_bbox_iou": ("min", 0.5),
    "retrieval_recall_at_8": ("min", 0.8),
    "no_answer_refusal_accuracy": ("min", 0.9),
    "citation_version_accuracy": ("min", 1.0),
    "table_cell_accuracy": ("min", 1.0),
    "image_ocr_recall": ("min", 1.0),
    "image_caption_recall": ("min", 1.0),
    "flowchart_node_recall": ("min", 1.0),
    "flowchart_edge_recall": ("min", 1.0),
    "flowchart_condition_recall": ("min", 1.0),
    "flowchart_warning_recall": ("min", 1.0),
    "vlm_description_recall": ("min", 1.0),
}
PDF_PARSER_PROFILE = "pdf_deepdoc_v1"
MIN_SAMPLE_COUNT = 16
MIN_QUERY_COUNT = 50
MIN_NEGATIVE_QUERY_COUNT = 5
REQUIRED_ENV = (
    "GATEWAY_URL",
    "RAGFLOW_BASE_URL",
    "RAGFLOW_API_KEY",
    "ENTERPRISE_SYNC_SERVICE_TOKEN",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "WP03_UNAUTHORIZED_USER_TOKEN",
)


class AcceptanceBlocked(RuntimeError):
    """A formal prerequisite is absent; the run is not a test failure."""


def _valid_bbox(value: Any) -> bool:
    if isinstance(value, dict):
        values = (
            value.get("x1", value.get("left")),
            value.get("x2", value.get("right")),
            value.get("y1", value.get("top")),
            value.get("y2", value.get("bottom")),
        )
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        values = value[:4]
    else:
        return False
    try:
        left, right, top, bottom = (float(item) for item in values)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(item) for item in (left, right, top, bottom))
        and 0 <= left <= right
        and 0 <= top <= bottom
    )


def _finite_metric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _formal_metric_threshold(thresholds: dict[str, Any], metric: str) -> tuple[str, float]:
    direction, default = FORMAL_METRIC_THRESHOLDS[metric]
    configured = thresholds.get(metric)
    if configured is None:
        configured = thresholds.get(f"{direction}_{metric}")
    value = _finite_metric(configured)
    if value is None:
        return direction, default
    # The phase-1 file is intentionally conservative only for synthetic data;
    # it must never weaken a formal customer gate.
    if direction == "min":
        return direction, max(default, value)
    return direction, min(default, value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_url(value: str) -> str:
    """Keep endpoint evidence without credentials, query parameters or fragments."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return "configured"
    if not parts.scheme or not parts.hostname:
        return "configured"
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AcceptanceBlocked(f"real S1-S8 manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceBlocked(f"real S1-S8 manifest is unreadable: {type(exc).__name__}") from exc
    if manifest.get("schema_version") != 1:
        raise AcceptanceBlocked("real S1-S8 manifest schema_version must be 1")
    try:
        from enterprise.scripts.wp03.collector import validate_manifest

        validate_manifest(manifest)
    except (ImportError, ValueError, TypeError) as exc:
        raise AcceptanceBlocked(f"real S1-S8 manifest violates evaluation contract: {exc}") from exc
    provenance = manifest.get("ground_truth_provenance") or {}
    if provenance.get("source") == "synthetic_generator" or provenance.get("human_reviewed") is not True:
        raise AcceptanceBlocked("formal S1-S8 ground truth must be sanitized and human reviewed")
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise AcceptanceBlocked("real S1-S8 manifest samples must be a list")
    counts = {scenario_id: 0 for scenario_id in SCENARIOS}
    query_count = 0
    negative_query_count = 0
    for sample in samples:
        scenario_id = sample.get("scenario_id")
        if scenario_id not in SCENARIOS:
            raise AcceptanceBlocked(f"formal manifest has unknown scenario_id: {scenario_id}")
        counts[scenario_id] += 1
        if sample.get("category") != SCENARIO_CATEGORIES[scenario_id]:
            raise AcceptanceBlocked(
                f"{scenario_id} category must be {SCENARIO_CATEGORIES[scenario_id]}"
            )
        if sample.get("parser_profile") != PDF_PARSER_PROFILE:
            raise AcceptanceBlocked(
                f"{scenario_id} PDF parser_profile must be {PDF_PARSER_PROFILE}"
            )
        missing = set(SCENARIOS[scenario_id]) - set(sample.get("acceptance_dimensions") or [])
        if missing:
            raise AcceptanceBlocked(f"{scenario_id} acceptance_dimensions missing: {','.join(sorted(missing))}")
        required = sample.get("required_capabilities")
        if not isinstance(required, list) or set(required) != set(
            SCENARIO_REQUIRED_CAPABILITIES[scenario_id]
        ):
            raise AcceptanceBlocked(
                f"{scenario_id} required_capabilities must match the formal scenario contract"
            )
        if not sample.get("file_name") or not sample.get("file_sha256"):
            raise AcceptanceBlocked(f"{scenario_id} must declare file_name and file_sha256")
        if not isinstance(sample.get("ground_truth_fields"), dict) or not sample["ground_truth_fields"]:
            raise AcceptanceBlocked(f"{scenario_id} must declare human-reviewed ground_truth_fields")
        fields = sample["ground_truth_fields"]
        for field in ("equipment_id", "document_type", "version"):
            if not str(fields.get(field) or "").strip():
                raise AcceptanceBlocked(f"{scenario_id} ground_truth_fields must declare {field}")
        questions = sample.get("citation_questions")
        if not isinstance(questions, list) or not questions:
            raise AcceptanceBlocked(f"{scenario_id} must declare at least one citation question")
        negative_questions = sample.get("negative_questions")
        if not isinstance(negative_questions, list) or not negative_questions:
            raise AcceptanceBlocked(f"{scenario_id} must declare at least one negative question")
        query_count += len(questions) + len(negative_questions)
        negative_query_count += len(negative_questions)
        for question in questions:
            if not isinstance(question, dict) or not question.get("question"):
                raise AcceptanceBlocked(f"{scenario_id} has an invalid citation question")
            try:
                expected_page = int(question.get("expected_page", 0))
            except (TypeError, ValueError):
                expected_page = 0
            if expected_page <= 0:
                raise AcceptanceBlocked(f"{scenario_id} has an invalid citation question")
            if expected_page > int(sample.get("pages") or 0):
                raise AcceptanceBlocked(f"{scenario_id} citation question page is outside the sample")
            expected_answer = question.get("expected_answer_contains")
            if not isinstance(expected_answer, list) or not expected_answer:
                raise AcceptanceBlocked(f"{scenario_id} query must declare expected_answer_contains")
            if not _valid_bbox(question.get("expected_bbox")):
                raise AcceptanceBlocked(f"{scenario_id} query must declare a valid expected_bbox")
        for question in negative_questions:
            if not isinstance(question, dict) or not str(question.get("question") or "").strip():
                raise AcceptanceBlocked(f"{scenario_id} has an invalid negative question")
        expected_quality = SCENARIO_EXPECTED_QUALITY[scenario_id]
        if sample.get("expected_quality_status") != expected_quality:
            raise AcceptanceBlocked(
                f"{scenario_id} expected_quality_status must be {expected_quality}"
            )
    if len(samples) < MIN_SAMPLE_COUNT:
        raise AcceptanceBlocked(f"formal manifest needs at least {MIN_SAMPLE_COUNT} samples")
    deficient = [
        f"{scenario_id}:{counts[scenario_id]}/{minimum}"
        for scenario_id, minimum in SCENARIO_MINIMUMS.items()
        if counts[scenario_id] < minimum
    ]
    if deficient:
        raise AcceptanceBlocked("formal scenario coverage is incomplete: " + ",".join(deficient))
    if query_count < MIN_QUERY_COUNT:
        raise AcceptanceBlocked(f"formal manifest needs at least {MIN_QUERY_COUNT} queries; found {query_count}")
    if negative_query_count < MIN_NEGATIVE_QUERY_COUNT:
        raise AcceptanceBlocked(
            "formal manifest needs at least "
            f"{MIN_NEGATIVE_QUERY_COUNT} negative queries; found {negative_query_count}"
        )
    return manifest


def preflight(manifest_path: Path, samples_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _load_manifest(manifest_path)
    sample_evidence: list[dict[str, Any]] = []
    for sample in manifest["samples"]:
        path = samples_dir / sample["file_name"]
        if not path.is_file() or path.stat().st_size == 0:
            raise AcceptanceBlocked(f"real fixture is missing or empty: {sample['scenario_id']}")
        actual_hash = _sha256(path)
        if actual_hash.lower() != str(sample["file_sha256"]).lower():
            raise AcceptanceBlocked(f"fixture SHA-256 mismatch: {sample['scenario_id']}")
        sample_evidence.append(
            {
                "scenario_id": sample["scenario_id"],
                "sample_id": sample.get("sample_id"),
                "file_name": sample["file_name"],
                "file_sha256": actual_hash,
                "acceptance_dimensions": list(sample["acceptance_dimensions"]),
                "expected_quality_status": sample["expected_quality_status"],
                "query_count": len(sample["citation_questions"])
                + len(sample["negative_questions"]),
                "negative_query_count": len(sample["negative_questions"]),
            }
        )
    missing_env = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing_env:
        raise AcceptanceBlocked("live environment missing: " + ",".join(missing_env))
    return manifest, sample_evidence


def _environment() -> dict[str, Any]:
    return {
        "gateway_url": _safe_url(os.environ.get("GATEWAY_URL", "")),
        "ragflow_base_url": _safe_url(os.environ.get("RAGFLOW_BASE_URL", "")),
        "s3_endpoint": _safe_url(os.environ.get("S3_ENDPOINT", "")),
        "s3_bucket": os.environ.get("S3_BUCKET", ""),
        "credential_presence": {name: bool(os.environ.get(name, "").strip()) for name in REQUIRED_ENV},
        "persistence_backend": "sqlite",
        "postgres_integration": "not_applicable",
        "postgres_reason": "no enterprise PostgreSQL runtime call path",
    }


def _write_junit(path: Path, cases: list[dict[str, str]], blocked: bool = False) -> None:
    failures = sum(case["status"] == "failed" for case in cases)
    errors = sum(case["status"] == "blocked" for case in cases)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        (
            f'<testsuite name="wp03-formal-acceptance" tests="{len(cases)}" '
            f'failures="{failures}" errors="{errors}" skipped="0">'
        ),
    ]
    for case in cases:
        lines.append(f"  <testcase classname=\"wp03.acceptance\" name={quoteattr(case['name'])}>")
        detail = escape(case.get("detail", ""))
        if case["status"] == "failed":
            lines.append(f'    <failure message="{detail}" />')
        elif case["status"] == "blocked" or blocked:
            lines.append(f'    <error message="{detail}" />')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _probe_acl(report: dict[str, Any]) -> list[dict[str, Any]]:
    import httpx

    base_url = os.environ["GATEWAY_URL"].rstrip("/")
    token = os.environ["WP03_UNAUTHORIZED_USER_TOKEN"]
    evidence: list[dict[str, Any]] = []
    with httpx.Client(timeout=15) as client:
        for document in report.get("documents") or []:
            external_id = document.get("external_document_id")
            if not external_id:
                evidence.append({"sample_id": document.get("sample_id"), "status": "failed", "reason": "external_document_id missing"})
                continue
            try:
                response = client.get(
                    f"{base_url}/enterprise/api/v1/documents/{external_id}/quality",
                    params={"source_system": "WP03"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                status = "passed" if response.status_code in (403, 404) else "failed"
                evidence.append(
                    {
                        "sample_id": document.get("sample_id"),
                        "external_document_id": external_id,
                        "http_status": response.status_code,
                        "status": status,
                         "reason": "unauthorized principal denied" if status == "passed" else "expected HTTP 403/404",
                    }
                )
            except httpx.HTTPError as exc:
                evidence.append(
                    {
                        "sample_id": document.get("sample_id"),
                        "external_document_id": external_id,
                        "status": "failed",
                        "reason": type(exc).__name__,
                    }
                )
    return evidence


def _metric_minimum(thresholds: dict[str, Any], name: str, default: float) -> float:
    try:
        return float(thresholds.get(name, default))
    except (TypeError, ValueError):
        return default


def _sample_failures(
    sample: dict[str, Any],
    document: dict[str, Any] | None,
    thresholds: dict[str, Any],
    acl_evidence: dict[str, dict[str, Any]],
) -> list[str]:
    if not document:
        return ["document evidence missing"]
    failures: list[str] = []
    metrics = document.get("metrics") or {}
    expected_status = sample["expected_quality_status"]
    if document.get("parse_quality_status") != expected_status:
        failures.append(
            f"quality expected={expected_status} actual={document.get('parse_quality_status')}"
        )
    if expected_status != "failed" and metrics.get("parse_success") is not True:
        failures.append("parse_success is not true")
    if document.get("source_cleanup") != "passed":
        failures.append("temporary source cleanup evidence missing or failed")
    parser = document.get("parser_application") or metrics.get("parserApplication")
    if expected_status != "failed":
        if not isinstance(parser, dict):
            failures.append("parser application evidence missing")
        else:
            if parser.get("state") != "executed":
                failures.append(f"parser application state={parser.get('state')}")
            for name in ("selectedProfile", "configuredProfile", "executedProfile"):
                if parser.get(name) != PDF_PARSER_PROFILE:
                    failures.append(f"parser application {name} mismatch")
            if parser.get("readbackMatch") is not True:
                failures.append("parser application readback mismatch")
    declared = metrics.get("required_capabilities")
    if set(declared or []) != set(sample.get("required_capabilities") or []):
        failures.append("required capability declaration mismatch")
    expectations = metrics.get("quality_expectations") or {}
    if expectations.get("declarations_complete") is not True:
        failures.append("quality expectation declarations are incomplete")
    if expected_status != "failed":
        out_of_range_pages = _finite_metric(metrics.get("out_of_range_page_count"))
        if out_of_range_pages is None or out_of_range_pages != 0.0:
            failures.append("out_of_range_page_count is non-zero")
        formal_basic_metrics = {
            "page_coverage": ("min", 1.0),
            "position_coverage": ("min", 0.95),
            "key_field_accuracy": ("min", 1.0),
        }
        if expected_status == "passed":
            formal_basic_metrics["effective_text_coverage"] = ("min", 0.9)
        for metric, (direction, default) in formal_basic_metrics.items():
            value = _finite_metric(metrics.get(metric))
            configured = _metric_minimum(
                thresholds,
                {
                    "page_coverage": "min_page_coverage",
                    "position_coverage": "min_position_coverage",
                    "key_field_accuracy": "min_key_field_accuracy",
                    "effective_text_coverage": "min_text_coverage",
                }[metric],
                default,
            )
            required = max(default, configured)
            if value is None or value < required:
                failures.append(f"{metric} below hard threshold")

        for metric in SCENARIO_REQUIRED_METRICS[sample["scenario_id"]]:
            value = _finite_metric(metrics.get(metric))
            if value is None:
                failures.append(f"{metric} evidence missing or non-numeric")
                continue
            direction, required = _formal_metric_threshold(thresholds, metric)
            if expected_status == "passed" and (
                (direction == "min" and value < required)
                or (direction == "max" and value > required)
            ):
                failures.append(f"{metric} below formal threshold" if direction == "min" else f"{metric} above formal threshold")
    if expected_status == "review_required" and not document.get("quality_reasons"):
        failures.append("review_required result has no quality reason")
    if sample.get("expected_tables"):
        table_recall = _finite_metric(metrics.get("table_recall"))
        if table_recall is None or table_recall < max(
            1.0, _metric_minimum(thresholds, "min_table_recall", 1.0)
        ):
            failures.append("table_recall below hard threshold")

    chunks = document.get("chunks") or []
    dimensions = set(sample.get("acceptance_dimensions") or [])
    if not dimensions:
        failures.append("acceptance_dimensions missing")
    if "table_crop" in dimensions and not any(
        chunk.get("image_id") and str(chunk.get("doc_type_kwd") or "").lower() == "table"
        for chunk in chunks
    ):
        failures.append("table crop evidence missing")
    if dimensions.intersection({"image_crop", "diagram_labels", "flowchart_nodes"}) and not any(
        chunk.get("image_id") and str(chunk.get("doc_type_kwd") or "").lower() == "image"
        for chunk in chunks
    ):
        failures.append("image/diagram crop evidence missing")

    citation_results = document.get("citation_results")
    expected_questions = [
        {**question, "expected_no_answer": False}
        for question in (sample.get("citation_questions") or [])
    ] + [
        {**question, "expected_no_answer": True}
        for question in (sample.get("negative_questions") or [])
    ]
    if not isinstance(citation_results, list) or len(citation_results) != len(expected_questions):
        failures.append("citation evidence count mismatch")
    else:
        for index, (question, citation) in enumerate(
            zip(expected_questions, citation_results), start=1
        ):
            if citation.get("error"):
                failures.append(f"query {index} returned {citation['error']}")
            if question["expected_no_answer"]:
                if citation.get("expected_no_answer") is not True:
                    failures.append(f"query {index} expected_no_answer=false")
                if citation.get("no_answer_refused") is not True:
                    failures.append(f"query {index} no_answer_refused=false")
                continue
            for field in (
                "matched",
                "position_valid",
                "scope_document_match",
                "answer_matched",
                "version_match",
            ):
                if citation.get(field) is not True:
                    failures.append(f"query {index} {field}=false")
            bbox_iou = _finite_metric(citation.get("bbox_iou"))
            if bbox_iou is None or bbox_iou < _formal_metric_threshold(
                thresholds, "citation_bbox_iou"
            )[1]:
                failures.append(f"query {index} bbox_iou below hard threshold")

    acl = acl_evidence.get(str(sample.get("sample_id")))
    if not acl or acl.get("status") != "passed":
        failures.append("ACL negative evidence missing or failed")
    return failures


def _verify_report(
    report_path: Path,
    manifest: dict[str, Any],
    acl_results: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if not report_path.is_file():
        return ([{"name": "evaluation-report", "status": "failed", "detail": "evaluation report missing"}], [])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    documents = {doc.get("sample_id"): doc for doc in report.get("documents") or []}
    acl_evidence = {str(item.get("sample_id")): item for item in acl_results}
    thresholds = report.get("thresholds") or {}
    cases: list[dict[str, str]] = []
    matrix: list[dict[str, Any]] = []
    for sample in manifest["samples"]:
        document = documents.get(sample.get("sample_id"))
        actual = document.get("parse_quality_status") if document else None
        expected = sample["expected_quality_status"]
        failures = _sample_failures(sample, document, thresholds, acl_evidence)
        status = "passed" if not failures else "failed"
        detail = "all hard evidence present" if not failures else "; ".join(failures)
        cases.append({"name": sample["scenario_id"], "status": status, "detail": detail})
        matrix.append(
            {
                "scenario_id": sample["scenario_id"],
                "sample_id": sample.get("sample_id"),
                "dimensions": sample["acceptance_dimensions"],
                "expected_quality_status": expected,
                "actual_quality_status": actual,
                "status": status,
                "quality_reasons": (document or {}).get("quality_reasons") or [],
                "metrics": (document or {}).get("metrics") or {},
                "position_evidence": [
                    {
                        "matched": result.get("matched"),
                        "position_valid": result.get("position_valid"),
                    }
                    for result in (document or {}).get("citation_results") or []
                ],
                "citation_evidence": (document or {}).get("citation_results") or [],
                "acl_evidence": acl_evidence.get(str(sample.get("sample_id"))),
                "failures": failures,
            }
        )
    artifact_hash = report.get("artifact_hash")
    if artifact_hash:
        from enterprise.scripts.wp03.report import json_digest

        unsigned_report = dict(report)
        unsigned_report.pop("artifact_hash", None)
        if artifact_hash != json_digest(unsigned_report):
            cases.append(
                {
                    "name": "evidence-integrity",
                    "status": "failed",
                    "detail": "artifact_hash does not match the report payload",
                }
            )
    if not artifact_hash or not report.get("summary", {}).get("e2e_repeatability_hash"):
        cases.append(
            {
                "name": "evidence-integrity",
                "status": "failed",
                "detail": "artifact_hash or e2e_repeatability_hash missing",
            }
        )
    return cases, matrix


def _verify_repeatability(first_path: Path, repeat_path: Path) -> dict[str, str]:
    """Require two independent fresh parses to have identical semantic hashes."""
    if not first_path.is_file() or not repeat_path.is_file():
        return {
            "name": "repeatability",
            "status": "failed",
            "detail": "repeat evaluation report missing",
        }
    try:
        first = json.loads(first_path.read_text(encoding="utf-8"))
        repeat = json.loads(repeat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "name": "repeatability",
            "status": "failed",
            "detail": f"repeat evaluation report unreadable: {type(exc).__name__}",
        }
    first_summary = first.get("summary") or {}
    repeat_summary = repeat.get("summary") or {}
    for key in ("parse_repeatability_hash", "e2e_repeatability_hash"):
        first_hash = first_summary.get(key)
        repeat_hash = repeat_summary.get(key)
        if not first_hash or not repeat_hash:
            return {
                "name": "repeatability",
                "status": "failed",
                "detail": f"{key} missing from one or both reports",
            }
        if first_hash != repeat_hash:
            return {
                "name": "repeatability",
                "status": "failed",
                "detail": f"{key} differs between fresh parses",
            }
    return {
        "name": "repeatability",
        "status": "passed",
        "detail": "parse and end-to-end hashes match across two fresh parses",
    }


def run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = artifact_dir / "evidence.json"
    matrix_path = artifact_dir / "capability-matrix.json"
    junit_path = Path(args.junit)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked",
        "manifest": str(Path(args.manifest)),
        "samples_dir": str(Path(args.samples_dir)),
        "environment": _environment(),
    }
    try:
        manifest, samples = preflight(Path(args.manifest), Path(args.samples_dir))
        evidence["manifest_sha256"] = _sha256(Path(args.manifest))
        evidence["samples"] = samples
    except AcceptanceBlocked as exc:
        evidence["reason"] = str(exc)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_junit(junit_path, [{"name": "formal-prerequisites", "status": "blocked", "detail": str(exc)}])
        return 2

    report_root = Path(
        os.environ.get("WP03_REPORT_ROOT", str(ROOT / "artifacts" / "wp03" / "reports"))
    )

    def evaluation_command(run_id: str) -> list[str]:
        return [
            sys.executable,
            str(ROOT / "enterprise" / "scripts" / "wp03" / "run_parsing_evaluation.py"),
            "--run-id",
            run_id,
            "--manifest",
            str(Path(args.manifest)),
            "--samples-dir",
            str(Path(args.samples_dir)),
            "--thresholds",
            str(Path(args.thresholds)),
            "--output-dir",
            str(report_root),
            "--fresh-parse",
        ]

    result = subprocess.run(
        evaluation_command(args.run_id),
        cwd=str(ROOT),
        timeout=args.timeout,
        check=False,
    )
    evidence["evaluation_exit_code"] = result.returncode
    if result.returncode != 0:
        evidence["status"] = "failed"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_junit(
            junit_path,
            [{"name": "evaluation-command", "status": "failed", "detail": f"exit={result.returncode}"}],
        )
        return 1

    report_path = report_root / args.run_id / "evaluation-report.json"
    repeat_run_id = f"{args.run_id}-repeat"
    repeat_result = subprocess.run(
        evaluation_command(repeat_run_id),
        cwd=str(ROOT),
        timeout=args.timeout,
        check=False,
    )
    evidence["repeat_evaluation_exit_code"] = repeat_result.returncode
    if repeat_result.returncode != 0:
        evidence["status"] = "failed"
        evidence["repeat_evaluation_report"] = str(
            report_root / repeat_run_id / "evaluation-report.json"
        )
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_junit(
            junit_path,
            [{
                "name": "repeat-evaluation-command",
                "status": "failed",
                "detail": f"exit={repeat_result.returncode}",
            }],
        )
        return 1

    repeat_report_path = report_root / repeat_run_id / "evaluation-report.json"
    if not report_path.is_file():
        acl_results: list[dict[str, Any]] = []
    else:
        parsed_report = json.loads(report_path.read_text(encoding="utf-8"))
        acl_results = _probe_acl(parsed_report)
    cases, matrix = _verify_report(report_path, manifest, acl_results)
    repeatability_case = _verify_repeatability(report_path, repeat_report_path)
    cases.append(repeatability_case)
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence["evaluation_report"] = str(report_path)
    evidence["repeat_evaluation_report"] = str(repeat_report_path)
    evidence["repeatability"] = repeatability_case
    evidence["matrix"] = str(matrix_path)
    evidence["acl_evidence"] = acl_results
    evidence["status"] = "passed" if all(case["status"] == "passed" for case in cases) else "failed"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_junit(junit_path, cases)
    return 0 if evidence["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default=os.environ.get("WP03_ACCEPTANCE_MANIFEST", "artifacts/wp03/real-acceptance/manifest.json"),
    )
    parser.add_argument(
        "--samples-dir",
        default=os.environ.get("WP03_ACCEPTANCE_FIXTURE_DIR", "artifacts/wp03/real-acceptance/samples"),
    )
    parser.add_argument("--thresholds", default="enterprise/scripts/wp03/thresholds.json")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--junit", required=True)
    parser.add_argument("--run-id", default=f"wp03-acceptance-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        artifact_dir = Path(args.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        detail = f"{type(exc).__name__}: {exc}"
        (artifact_dir / "evidence.json").write_text(
            json.dumps({"schema_version": 1, "status": "error", "reason": detail}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_junit(Path(args.junit), [{"name": "acceptance-runner", "status": "failed", "detail": detail}])
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
