"""Shared document-level retrieval readiness for sync status and formal query."""

from __future__ import annotations

import os
from dataclasses import dataclass

import aiosqlite

from enterprise.gateway.quality.gate import enforce_quality_gate
from enterprise.gateway.quality.models import get_latest_evaluation
from enterprise.gateway.quality.routing import parser_application_readback_match
from enterprise.gateway.sync.models import ExtDocumentMap


_FILE_SHARE_PIPELINE_DONE = frozenset({"DONE", "3"})


@dataclass(frozen=True)
class DocumentCandidateReadiness:
    """Document facts only; user ACL is deliberately evaluated elsewhere."""

    retrievable: bool
    blocking_reason: str | None
    current_version: bool
    active: bool
    sync_ready: bool
    parser_readback: bool
    ragflow_ids_present: bool
    quality_allowed: bool


def query_quality_required() -> bool:
    """Keep the existing production/test quality switch in one place."""

    default = "false" if os.environ.get("ENTERPRISE_TEST_MODE") == "1" else "true"
    return os.environ.get("ENTERPRISE_QUERY_QUALITY_REQUIRED", default).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def document_candidate_readiness(
    doc: ExtDocumentMap,
    *,
    quality_allowed: bool,
    quality_required: bool,
    quality_reason: str | None = None,
) -> DocumentCandidateReadiness:
    """Evaluate the same document gate used before formal RAGFlow retrieval.

    FILE_SHARE versions carry stricter facts because their registration can
    create RAGFlow IDs before parser/readback and version promotion finish.
    ACL is intentionally absent: this result is a document candidate fact,
    not a user authorization decision.
    """

    is_file_share = doc.source_kind == "FILE_SHARE"
    quality_required = quality_required or is_file_share
    current_version = bool(doc.current_version)
    active = doc.business_status == "active"
    sync_ready = doc.sync_status == "ready"
    parser_readback = (
        parser_application_readback_match(doc)
        if is_file_share
        else True
    )
    ragflow_ids_present = bool(doc.ragflow_dataset_id and doc.ragflow_document_id)

    blocking_reason: str | None = None
    if not current_version:
        blocking_reason = "DOCUMENT_NOT_CURRENT_VERSION"
    elif not active:
        blocking_reason = "DOCUMENT_NOT_ACTIVE"
    elif not sync_ready:
        blocking_reason = "SYNC_NOT_READY"
    elif not parser_readback:
        blocking_reason = "PARSER_READBACK_NOT_READY"
    elif is_file_share and str(doc.pipeline_status or "").upper() not in _FILE_SHARE_PIPELINE_DONE:
        blocking_reason = "RAGFLOW_READBACK_NOT_READY"
    elif is_file_share and doc.event_status != "completed":
        blocking_reason = "SYNC_EVENT_NOT_COMPLETED"
    elif is_file_share and doc.source_state != "AVAILABLE":
        blocking_reason = "SOURCE_NOT_READY"
    elif not ragflow_ids_present:
        blocking_reason = "RAGFLOW_IDS_MISSING"
    elif quality_required and not quality_allowed:
        blocking_reason = quality_reason or "DOCUMENT_QUALITY_PENDING"

    return DocumentCandidateReadiness(
        retrievable=blocking_reason is None,
        blocking_reason=blocking_reason,
        current_version=current_version,
        active=active,
        sync_ready=sync_ready,
        parser_readback=parser_readback,
        ragflow_ids_present=ragflow_ids_present,
        quality_allowed=quality_allowed,
    )


async def document_candidate_readiness_from_db(
    db: aiosqlite.Connection, doc: ExtDocumentMap,
) -> tuple[DocumentCandidateReadiness, str | None]:
    """Load quality evidence and evaluate the shared document gate."""

    quality_required = query_quality_required() or doc.source_kind == "FILE_SHARE"
    evaluation = await get_latest_evaluation(
        db,
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )
    if evaluation is not None:
        quality_allowed, quality_reason = enforce_quality_gate(evaluation)
    elif quality_required:
        quality_allowed, quality_reason = False, "DOCUMENT_QUALITY_PENDING"
    else:
        quality_allowed, quality_reason = True, None
    quality_gate_required = quality_required or evaluation is not None
    return (
        document_candidate_readiness(
            doc,
            quality_allowed=quality_allowed,
            quality_required=quality_gate_required,
            quality_reason=quality_reason,
        ),
        evaluation.parse_quality_status if evaluation else None,
    )
