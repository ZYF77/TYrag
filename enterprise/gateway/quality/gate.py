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

CAPABILITY_DIMENSIONS = {
    "text": "text_quality",
    "table": "table_quality",
    "position": "position_quality",
    "key_field": "key_field_quality",
    "citation": "citation_quality",
    "image_semantic": "image_semantic_quality",
}


def required_quality_dimensions(
    metrics: dict[str, Any] | None,
) -> tuple[tuple[str, ...], bool]:
    """Resolve declared required capabilities to quality dimensions.

    The boolean is false for malformed or unknown declarations so callers can
    fail closed instead of silently treating a required capability as optional.
    """
    metrics = metrics or {}
    raw = metrics.get("required_capabilities")
    if raw is None:
        raw = metrics.get("required_quality_dimensions")
    if raw is None:
        return (), False
    if not isinstance(raw, (list, tuple, set)):
        return (), False

    expectations = metrics.get("quality_expectations")
    if (
        not isinstance(expectations, dict)
        or expectations.get("declarations_complete") is not True
    ):
        return (), False

    resolved: list[str] = []
    valid = True
    for value in raw:
        name = str(value or "").strip().lower()
        dimension = (
            name if name in QUALITY_DIMENSIONS else CAPABILITY_DIMENSIONS.get(name)
        )
        if dimension is None:
            valid = False
            continue
        if dimension not in resolved:
            resolved.append(dimension)
    return tuple(resolved), valid and bool(resolved)


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


def _parser_application_verified(metrics: dict[str, Any] | None) -> bool:
    """Return true only after the selected profile was read back post-parse."""
    metrics = metrics or {}
    snapshot = metrics.get("parserApplication") or metrics.get("parser_application")
    return (
        isinstance(snapshot, dict)
        and snapshot.get("state") == "executed"
        and snapshot.get("readbackMatch") is True
    )


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
        if not _parser_application_verified(
            getattr(evaluation, "metrics_json", None)
        ):
            if not strict_mode and demo_warn_mode:
                return True, "DOCUMENT_QUALITY_WARN"
            return False, "DOCUMENT_REVIEW_REQUIRED"
        required, declaration_valid = required_quality_dimensions(
            getattr(evaluation, "metrics_json", None)
        )
        if not declaration_valid:
            if not strict_mode and demo_warn_mode:
                return True, "DOCUMENT_QUALITY_WARN"
            return False, "DOCUMENT_REVIEW_REQUIRED"
        if required:
            dimensions = quality_dimensions(
                getattr(evaluation, "metrics_json", None)
            )
            if any(dimensions.get(name) != "passed" for name in required):
                if not strict_mode and demo_warn_mode:
                    return True, "DOCUMENT_QUALITY_WARN"
                return False, "DOCUMENT_REVIEW_REQUIRED"
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
