"""Shared document-level retrieval readiness for sync status and formal query."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import fetchall
from enterprise.gateway.quality.gate import enforce_quality_gate
from enterprise.gateway.quality.models import (
    QualityEvaluation,
    get_latest_evaluation,
    _row_to_evaluation,
)
from enterprise.gateway.sync.models import ExtDocumentMap

_MANAGED_FEED_PIPELINE_DONE = frozenset({"DONE", "3"})


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


def evaluation_lookup_key(
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str | None,
) -> tuple[str, str, str, str]:
    """Stable key for latest-evaluation maps used by scope/readiness."""

    return (
        str(tenant_id or ""),
        str(source_system or ""),
        str(external_document_id or ""),
        str(source_version_id or ""),
    )


def document_evaluation_key(doc: ExtDocumentMap) -> tuple[str, str, str, str]:
    return evaluation_lookup_key(
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )


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

    is_managed_feed = doc.source_kind in {"FILE_SHARE", "INLINE_JSON"}
    quality_required = quality_required or is_managed_feed
    current_version = bool(doc.current_version)
    active = doc.business_status == "active"
    sync_ready = doc.sync_status == "ready"
    parser_readback = (
        str(doc.pipeline_status or "").upper() in _MANAGED_FEED_PIPELINE_DONE
        if is_managed_feed
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
        blocking_reason = "RAGFLOW_READBACK_NOT_READY"
    elif is_managed_feed and doc.event_status != "completed":
        blocking_reason = "SYNC_EVENT_NOT_COMPLETED"
    elif is_managed_feed and doc.source_state != "AVAILABLE":
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


def document_candidate_readiness_from_evaluation(
    doc: ExtDocumentMap,
    evaluation: QualityEvaluation | None,
) -> tuple[DocumentCandidateReadiness, str | None]:
    """Evaluate readiness from an already-loaded evaluation (no DB I/O)."""

    quality_required = query_quality_required() or doc.source_kind in {
        "FILE_SHARE",
        "INLINE_JSON",
    }
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


async def get_latest_evaluations_batch(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    source_system: str | None = None,
    docs: Iterable[ExtDocumentMap] | None = None,
) -> dict[tuple[str, str, str, str], QualityEvaluation]:
    """Load latest parse_quality_evaluation rows in one query.

    Semantics match repeated ``get_latest_evaluation(..., source_version_id=...)``
    calls: for each (tenant, source_system, external_document_id, source_version_id)
    keep the row with the greatest ``id``.
    """

    params: list[object] = [tenant_id]
    clauses = ["tenant_id=?"]
    if source_system is not None:
        clauses.append("source_system=?")
        params.append(source_system)

    rows = await fetchall(
        conn,
        f"""SELECT DISTINCT ON (tenant_id, source_system, external_document_id, source_version_id)
                   *
            FROM parse_quality_evaluation
            WHERE {" AND ".join(clauses)}
            ORDER BY tenant_id, source_system, external_document_id, source_version_id, id DESC""",
        params,
    )
    out: dict[tuple[str, str, str, str], QualityEvaluation] = {
        evaluation_lookup_key(
            row["tenant_id"],
            row["source_system"],
            row["external_document_id"],
            row["source_version_id"],
        ): _row_to_evaluation(row)
        for row in rows
    }
    if docs is None:
        return out
    doc_list = list(docs)
    if not doc_list:
        return {}
    wanted = {document_evaluation_key(doc) for doc in doc_list}
    return {key: value for key, value in out.items() if key in wanted}


async def document_candidate_readiness_from_db(
    conn: AsyncConnection,
    doc: ExtDocumentMap,
) -> tuple[DocumentCandidateReadiness, str | None]:
    """Load quality evidence and evaluate the shared document gate."""

    evaluation = await get_latest_evaluation(
        conn,
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )
    return document_candidate_readiness_from_evaluation(doc, evaluation)
