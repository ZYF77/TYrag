"""Configurable parse quality gate independent of WP-02 sync status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_chunk_count": 1,
    "empty_page_ratio_max": 0.1,
    "min_text_coverage": 0.9,
    "max_garbled_ratio": 0.01,
    "min_page_coverage": 0.9,
    "min_position_coverage": 0.9,
    "min_table_recall": 1.0,
    "min_key_field_accuracy": 1.0,
    "min_citation_page_accuracy": 0.9,
}

THRESHOLD_META_KEYS = {
    "schema_version",
    "threshold_version",
    "phase",
    "temporary_conservative",
    "description",
}


def load_thresholds(path: str | Path | None = None) -> dict[str, Any]:
    if not path:
        return dict(DEFAULT_THRESHOLDS)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    thresholds: dict[str, Any] = dict(DEFAULT_THRESHOLDS)
    for key, value in data.items():
        if key in THRESHOLD_META_KEYS:
            thresholds[key] = value
        elif key in DEFAULT_THRESHOLDS:
            thresholds[key] = float(value)
    return thresholds


def evaluate_document_quality(
    metrics: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    expected_tables: list[int] | None = None,
    ground_truth_fields: dict[str, str] | None = None,
    citation_expected: bool = False,
) -> tuple[str, list[str]]:
    """Return (parse_quality_status, reasons) without mutating sync_status."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    reasons: list[str] = []

    if not metrics.get("parse_success"):
        return "failed", ["RAGFLOW_PARSE_FAILED"]
    if metrics.get("chunk_count", 0) < thresholds["min_chunk_count"]:
        reasons.append("CHUNK_COUNT_BELOW_MIN")
    if metrics.get("empty_page_ratio", 0.0) > thresholds["empty_page_ratio_max"]:
        reasons.append("EMPTY_PAGE_RATIO_ABOVE_MAX")
    if metrics.get("effective_text_coverage", 0.0) < thresholds["min_text_coverage"]:
        reasons.append("TEXT_COVERAGE_BELOW_MIN")
    if metrics.get("garbled_char_ratio", 0.0) > thresholds["max_garbled_ratio"]:
        reasons.append("GARBLED_RATIO_ABOVE_MAX")
    if metrics.get("page_coverage", 0.0) < thresholds["min_page_coverage"]:
        reasons.append("PAGE_COVERAGE_BELOW_MIN")
    if metrics.get("position_coverage", 0.0) < thresholds["min_position_coverage"]:
        reasons.append("POSITION_COVERAGE_BELOW_MIN")
    if expected_tables:
        table_recall = metrics.get("table_recall")
        if table_recall is None or table_recall < thresholds["min_table_recall"]:
            reasons.append("TABLE_RECALL_BELOW_MIN")
    if ground_truth_fields:
        accuracy = metrics.get("key_field_accuracy")
        if accuracy is None or accuracy < thresholds["min_key_field_accuracy"]:
            reasons.append("KEY_FIELD_ACCURACY_BELOW_MIN")
    if citation_expected:
        accuracy = metrics.get("citation_page_accuracy")
        if accuracy is None or accuracy < thresholds["min_citation_page_accuracy"]:
            reasons.append("CITATION_PAGE_ACCURACY_BELOW_MIN")

    if reasons:
        return "review_required", reasons
    return "passed", ["PASSED"]
