"""Normalize source-owned document classification values for FILE_SHARE."""

from __future__ import annotations


def _optional_code(metadata: dict, key: str) -> str | None:
    value = metadata.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError(f"{key} must be a short string")
    return value.strip().upper() or None


def validate_document_classification(metadata: dict) -> tuple[str, str | None, str | None]:
    """Validate and preserve the source-owned document type value."""

    document_type = metadata.get("document_type")
    if not isinstance(document_type, str):
        raise ValueError("document_type is required")
    document_type = document_type.strip()
    if not document_type:
        raise ValueError("document_type is required")
    if len(document_type) > 64:
        raise ValueError("document_type must be at most 64 characters")
    return (
        document_type,
        _optional_code(metadata, "document_subtype"),
        _optional_code(metadata, "source_document_type"),
    )
