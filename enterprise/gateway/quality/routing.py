"""Deterministic, document-scoped parser profiles for enterprise ingestion."""

from __future__ import annotations

import json
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


def _profile_definition(name: str, version: str) -> dict[str, Any]:
    """Return one immutable-by-convention server profile definition.

    Profile names are versioned identifiers.  A future definition must use a
    new name (for example ``pdf_deepdoc_v2``) instead of changing the meaning
    of an already persisted ``*_v1`` document.
    """
    profile = PARSER_PROFILES.get(name)
    if profile is None or str(profile.get("parser_version")) != str(version):
        raise ValueError(
            f"Parser profile version is unavailable: {name}@{version}"
        )
    return {
        **profile,
        "parser_config": dict(profile.get("parser_config") or {}),
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
    # Keep the parameter for compatibility with older internal callers, but
    # never let request data select a profile.  The server owns this decision.
    reasons: list[str] = (
        ["CLIENT_PROFILE_OVERRIDE_IGNORED"] if manual_profile else []
    )
    lower_name = (file_name or "").lower()
    lower_media = (media_type or "").lower()
    is_pdf = lower_media == "application/pdf" or lower_name.endswith(".pdf")
    is_image = lower_media.startswith("image/")
    is_tabular = lower_media in (
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ) or lower_name.endswith(_TABULAR_SUFFIXES)

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
        **_profile_definition(profile_name, str(profile["parser_version"])),
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "routing_reasons": reasons,
        "category": category,
        "whether_manual_override": False,
        "client_override_ignored": bool(manual_profile),
        "api_application_status": "selected",
    }


def route_document_for_mapping(doc: Any) -> dict[str, Any]:
    """Route a new mapping or preserve its server-selected profile version."""
    name = getattr(doc, "parser_profile", None)
    version = getattr(doc, "parser_profile_version", None)
    if name or version:
        if not name or not version:
            raise ValueError("Persisted parser profile is incomplete")
        profile = _profile_definition(name, str(version))
        return {
            "selected_parser_profile": name,
            **profile,
            "parser_version": str(version),
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "routing_reasons": ["PERSISTED_PROFILE_VERSION"],
            "category": "persisted_profile",
            "whether_manual_override": False,
            "client_override_ignored": False,
            "api_application_status": "selected",
        }
    routing = route_document(
        media_type=doc.media_type,
        file_name=doc.file_name,
        document_type=doc.document_type,
        source_system=doc.source_system,
    )
    return routing


def parser_application_readback_match(doc: Any) -> bool:
    """Verify persisted parser evidence instead of trusting state alone."""
    if getattr(doc, "parser_application_status", None) != "executed":
        return False
    if not getattr(doc, "parser_profile", None) or not getattr(
        doc, "parser_profile_version", None
    ):
        return False
    try:
        expected = json.loads(doc.parser_expected_json or "")
        executed = json.loads(doc.parser_executed_json or "")
    except (TypeError, ValueError):
        return False
    if not isinstance(expected, dict) or not isinstance(executed, dict):
        return False
    if expected.get("profile") != doc.parser_profile:
        return False
    if str(expected.get("profile_version")) != str(doc.parser_profile_version):
        return False
    evidence_keys = (
        "profile",
        "profile_version",
        "policy_version",
        "chunk_method",
        "owned_parser_config",
    )
    if any(
        key not in expected or key not in executed for key in evidence_keys
    ):
        return False
    return all(
        expected.get(key) == executed.get(key)
        for key in evidence_keys
    )


def parser_configuration_matches(
    routing: dict[str, Any], ragflow_doc: dict[str, Any],
) -> bool:
    """Compare only the server-owned parser settings in a RAGFlow readback."""
    if not isinstance(ragflow_doc, dict):
        return False
    if str(ragflow_doc.get("chunk_method") or "").lower() != str(
        routing["chunk_method"]
    ).lower():
        return False
    parser_config = ragflow_doc.get("parser_config")
    if not isinstance(parser_config, dict):
        return False
    return all(
        parser_config.get(key) == value
        for key, value in (routing.get("parser_config") or {}).items()
    )
