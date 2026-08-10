"""Stable document classification codes for the v3 FILE_SHARE contract."""

from __future__ import annotations

DOCUMENT_TYPE_CODES = frozenset(
    {
        "ACCEPTANCE",
        "PRODUCT_MANUAL",
        "OPM_MANUAL",
        "CERTIFICATE",
        "DRAWING",
        "GLASS_INTEGRITY",
        "COMMISSIONING",
        "URS",
        "REPAIR_RECORD",
        "MAINTENANCE_RECORD",
        "ASSET_LIFECYCLE",
        "OTHER",
    }
)


def _optional_code(metadata: dict, key: str) -> str | None:
    value = metadata.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError(f"{key} must be a short string")
    return value.strip().upper() or None


def validate_document_classification(metadata: dict) -> tuple[str, str | None, str | None]:
    """Validate explicit classification; this function never performs AI classification."""

    document_type = metadata.get("document_type")
    if not isinstance(document_type, str):
        raise ValueError("document_type is required")
    document_type = document_type.strip().upper()
    if document_type not in DOCUMENT_TYPE_CODES:
        raise ValueError("document_type is not a supported catalog code")
    return (
        document_type,
        _optional_code(metadata, "document_subtype"),
        _optional_code(metadata, "source_document_type"),
    )
