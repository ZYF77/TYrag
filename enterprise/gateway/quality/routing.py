"""Deterministic parser routing policy for enterprise documents.

The route is recorded for auditability. RAGFlow v0.26.4's public upload API
does not accept a document-level ``chunk_method``, so the adapter reports
``api_application_status="recorded_only"`` instead of claiming the profile was
applied.
"""

from __future__ import annotations

from typing import Any

ROUTING_POLICY_VERSION = "1"

CATEGORY_PROFILES: dict[str, str] = {
    "digital_text": "naive",
    "scanned_document": "picture",
    "mixed_document": "naive",
    "table_dense": "table",
    "image_or_diagram_dense": "picture",
}


def route_document(
    *,
    media_type: str,
    file_name: str,
    document_type: str | None = None,
    source_system: str | None = None,
    manual_profile: str | None = None,
) -> dict[str, Any]:
    """Return a reproducible parser routing decision with audit reasons."""
    reasons: list[str] = []
    if manual_profile:
        category = "manual_override"
        reasons.append("MANUAL_OVERRIDE")
        return {
            "selected_parser_profile": manual_profile,
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "routing_reasons": reasons,
            "category": category,
            "whether_manual_override": True,
            "api_application_status": "recorded_only",
        }

    lower_name = (file_name or "").lower()
    lower_type = (document_type or "").lower()
    reasons.append(f"media_type={media_type or 'unknown'}")
    if source_system:
        reasons.append(f"source_system={source_system}")

    if lower_type in ("scanned", "scan", "scanned_document"):
        category = "scanned_document"
        reasons.append("document_type_scan")
    elif lower_type in ("table", "tables", "table_dense", "form"):
        category = "table_dense"
        reasons.append("document_type_table")
    elif any(marker in lower_name for marker in ("scan", "scanned", "影像")):
        category = "scanned_document"
        reasons.append("file_name_scan")
    elif any(marker in lower_name for marker in ("diagram", "image", "图", "flowchart")):
        category = "image_or_diagram_dense"
        reasons.append("file_name_image_or_diagram")
    elif any(marker in lower_name for marker in ("table", "表格", "form")):
        category = "table_dense"
        reasons.append("file_name_table")
    elif any(marker in lower_name for marker in ("mixed", "混合")):
        category = "mixed_document"
        reasons.append("file_name_mixed")
    else:
        category = "digital_text"
        reasons.append("default_digital_text")

    return {
        "selected_parser_profile": CATEGORY_PROFILES[category],
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "routing_reasons": reasons,
        "category": category,
        "whether_manual_override": False,
        "api_application_status": "recorded_only",
    }
