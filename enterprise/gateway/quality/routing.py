"""Deterministic, document-scoped parser profiles for enterprise ingestion."""

from __future__ import annotations

from typing import Any

ROUTING_POLICY_VERSION = "2"

PARSER_PROFILES: dict[str, dict[str, Any]] = {
    "pdf_deepdoc_v1": {
        "parser_version": "1",
        "chunk_method": "naive",
        "parser_config": {"layout_recognize": "DeepDOC"},
    },
    "image_picture_v1": {
        "parser_version": "1",
        "chunk_method": "picture",
        "parser_config": {},
    },
    "tabular_table_v1": {
        "parser_version": "1",
        "chunk_method": "table",
        "parser_config": {},
    },
}

_TABULAR_SUFFIXES = (".csv", ".xls", ".xlsx")


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
    lower_name = (file_name or "").lower()
    lower_media = (media_type or "").lower()
    is_pdf = lower_media == "application/pdf" or lower_name.endswith(".pdf")
    is_image = lower_media.startswith("image/")
    is_tabular = lower_media in (
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ) or lower_name.endswith(_TABULAR_SUFFIXES)

    if manual_profile:
        if manual_profile not in PARSER_PROFILES:
            raise ValueError(f"Unknown parser profile: {manual_profile}")
        if is_pdf and manual_profile != "pdf_deepdoc_v1":
            raise ValueError("PDF documents must use pdf_deepdoc_v1")
        category = "manual_override"
        reasons.append("MANUAL_OVERRIDE")
        profile = PARSER_PROFILES[manual_profile]
        return {
            "selected_parser_profile": manual_profile,
            **profile,
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "routing_reasons": reasons,
            "category": category,
            "whether_manual_override": True,
            "api_application_status": "selected",
        }

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

    if is_image:
        category = "image_or_diagram_dense"
        profile_name = "image_picture_v1"
        reasons.append("image_media_type")
    elif is_tabular and not is_pdf:
        category = "table_dense"
        profile_name = "tabular_table_v1"
        reasons.append("tabular_file_type")
    else:
        # DeepDOC is the safe PDF route for digital, scanned, mixed, table and
        # diagram pages. RAGFlow's picture/table chunk methods are not PDF parsers.
        profile_name = "pdf_deepdoc_v1"
        reasons.append("pdf_deepdoc_profile")
    profile = PARSER_PROFILES[profile_name]
    return {
        "selected_parser_profile": profile_name,
        **profile,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "routing_reasons": reasons,
        "category": category,
        "whether_manual_override": False,
        "api_application_status": "selected",
    }
