"""Query-time parse quality gate and dimension summaries."""

from __future__ import annotations

from typing import Any

from enterprise.gateway.quality.models import QualityEvaluation

QUALITY_DIMENSIONS = (
    "text_quality",
    "table_quality",
    "position_quality",
    "key_field_quality",
    "citation_quality",
    "image_semantic_quality",
)


def quality_dimensions(metrics: dict[str, Any] | None) -> dict[str, str]:
    """Return conservative per-dimension statuses without claiming unevaluated
    dimensions passed."""
    metrics = metrics or {}
    parse_success = bool(metrics.get("parse_success"))
    if not parse_success:
        return {name: "failed" for name in QUALITY_DIMENSIONS}

    chunk_count = int(metrics.get("chunk_count") or 0)
    text = "review_required"
    if chunk_count > 0:
        text = (
            "passed"
            if float(metrics.get("effective_text_coverage") or 0) >= 0.9
            and float(metrics.get("garbled_char_ratio") or 0) <= 0.01
            else "review_required"
        )
    elif chunk_count == 0:
        text = "not_evaluated"

    table_recall = metrics.get("table_recall")
    table = (
        "not_applicable"
        if table_recall is None
        else ("passed" if float(table_recall) >= 1.0 else "review_required")
    )

    position = (
        "review_required"
        if int(metrics.get("out_of_range_page_count") or 0) > 0
        or float(metrics.get("position_coverage") or 0) < 0.9
        else "passed"
    )

    key_field_accuracy = metrics.get("key_field_accuracy")
    key_field = (
        "not_evaluated"
        if key_field_accuracy is None
        else ("passed" if float(key_field_accuracy) >= 1.0 else "review_required")
    )

    citation_accuracy = metrics.get("citation_page_accuracy")
    citation = (
        "not_evaluated"
        if citation_accuracy is None
        else ("passed" if float(citation_accuracy) >= 0.9 else "review_required")
    )

    image_count = int(metrics.get("image_chunk_count") or 0)
    image = "not_applicable" if image_count == 0 else "not_evaluated"

    return {
        "text_quality": text,
        "table_quality": table,
        "position_quality": position,
        "key_field_quality": key_field,
        "citation_quality": citation,
        "image_semantic_quality": image,
    }


def enforce_quality_gate(
    evaluation: QualityEvaluation | None,
    *,
    strict_mode: bool = True,
    demo_warn_mode: bool = False,
) -> tuple[bool, str | None]:
    """Return (allowed, error_code). Fail closed unless warn mode is explicit."""
    if (
        evaluation is None
        or evaluation.evaluation_state != "completed"
        or evaluation.parse_quality_status is None
    ):
        if not strict_mode and demo_warn_mode:
            return True, "DOCUMENT_QUALITY_WARN"
        return False, "DOCUMENT_QUALITY_PENDING"

    status = evaluation.parse_quality_status
    if status == "passed":
        return True, None
    if status == "review_required":
        if not strict_mode and demo_warn_mode:
            return True, "DOCUMENT_QUALITY_WARN"
        return False, "DOCUMENT_REVIEW_REQUIRED"
    if status == "failed":
        if not strict_mode and demo_warn_mode:
            return True, "DOCUMENT_QUALITY_WARN"
        return False, "DOCUMENT_QUALITY_FAILED"
    if not strict_mode and demo_warn_mode:
        return True, "DOCUMENT_QUALITY_WARN"
    return False, "DOCUMENT_QUALITY_PENDING"


def safe_metric_summary(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded metric summary safe for end-user APIs."""
    metrics = metrics or {}
    return {
        "chunkCount": metrics.get("chunk_count"),
        "pageCoverage": metrics.get("page_coverage"),
        "positionCoverage": metrics.get("position_coverage"),
        "tableRecall": metrics.get("table_recall"),
        "keyFieldAccuracy": metrics.get("key_field_accuracy"),
        "citationPageAccuracy": metrics.get("citation_page_accuracy"),
    }
