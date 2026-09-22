"""EAM inbound quality confirm (manual override of review_required)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from enterprise.gateway.db.ops import gw_read, gw_write
from enterprise.gateway.quality import models as quality_models
from enterprise.gateway.sync.models import get_mapping, get_versions_for_document
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError
from enterprise.gateway.sync.sync_service import (
    DocumentSyncError,
    RetryableDocumentSyncError,
    promote_quality_passed_version,
)

logger = logging.getLogger(__name__)

CONFIRMABLE_STATUSES = frozenset({"review_required"})


class ConfirmQualityError(Exception):
    def __init__(self, http_status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ConfirmQualityResult:
    external_document_id: str
    source_version_id: str
    parse_quality_status: str
    current_version: int
    business_status: str
    promoted: bool
    idempotent: bool
    decision_id: str | None
    actor: str
    reason: str


def _is_retrievable(doc) -> bool:
    return (
        int(getattr(doc, "current_version", 0) or 0) >= 1
        and getattr(doc, "business_status", None) == "active"
        and getattr(doc, "sync_status", None) == "ready"
    )


async def confirm_document_quality(
    db,
    *,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    actor: str,
    reason: str,
    decision_id: str | None,
    ragflow_client: Any,
) -> ConfirmQualityResult:
    """Force review_required -> passed, then promote via existing path.

    Idempotent when already passed and already current/retrievable.
    Rejects disabled documents; non-latest versions return CONFLICT (409).
    """
    actor = (actor or "").strip()
    reason = (reason or "").strip()
    if not source_system or not source_version_id:
        raise ConfirmQualityError(
            422, "VALIDATION_ERROR", "source_system and source_version_id are required",
        )
    if not actor or not reason:
        raise ConfirmQualityError(
            422, "VALIDATION_ERROR", "actor and reason are required and must be non-empty",
        )

    doc = await gw_read(
        db, get_mapping, tenant_id, source_system, external_document_id, source_version_id,
    )
    if doc is None:
        raise ConfirmQualityError(404, "NOT_FOUND", "Document not found")

    if getattr(doc, "business_status", None) == "disabled":
        raise ConfirmQualityError(
            409,
            "REVIEW_ALREADY_CLOSED",
            "Document is disabled; confirm cannot release it",
        )

    evaluation = await gw_read(
        db,
        quality_models.get_latest_evaluation,
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )

    status = evaluation.parse_quality_status if evaluation else None

    # Idempotent success: already passed + already current/retrievable
    if status == "passed" and _is_retrievable(doc):
        logger.info(
            "quality confirm idempotent hit tenant_id=%s external_document_id=%s "
            "source_version_id=%s decision_id=%s actor=%s",
            doc.tenant_id,
            doc.external_document_id,
            doc.source_version_id,
            decision_id,
            actor,
        )
        return ConfirmQualityResult(
            external_document_id=doc.external_document_id,
            source_version_id=doc.source_version_id,
            parse_quality_status="passed",
            current_version=int(doc.current_version or 0),
            business_status=doc.business_status,
            promoted=True,
            idempotent=True,
            decision_id=decision_id,
            actor=actor,
            reason=reason,
        )

    if status not in CONFIRMABLE_STATUSES:
        raise ConfirmQualityError(
            409,
            "CONFLICT",
            f"Document quality status {status!r} is not confirmable "
            f"(expected review_required, or passed+retrievable for idempotent replay)",
        )

    # Non-latest -> 409 before mutating (avoid dual-current races where possible)
    versions = await gw_read(
        db,
        get_versions_for_document,
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
    )
    eligible = [
        v
        for v in versions
        if getattr(v, "business_status", None) not in {"disabled", "deleted"}
    ]
    latest = max(eligible, key=lambda v: int(v.id or 0), default=None)
    if latest is not None and int(latest.id or 0) != int(doc.id or 0):
        raise ConfirmQualityError(
            409,
            "CONFLICT",
            "Document version is not the latest eligible version; refuse dual current",
        )

    if evaluation is None:
        raise ConfirmQualityError(
            409,
            "CONFLICT",
            "No quality evaluation exists to confirm",
        )

    audit_metrics = dict(evaluation.metrics_json or {})
    audit_metrics["manualConfirm"] = {
        "actor": actor,
        "reason": reason,
        "decisionId": decision_id,
        "overrideFrom": status,
        "overrideTo": "passed",
    }
    reasons = list(evaluation.quality_reasons or [])
    reasons.append(f"manual_confirm:{actor}")

    await gw_write(
        db,
        quality_models.complete_evaluation,
        evaluation.id,
        parse_quality_status="passed",
        quality_reasons=reasons,
        metrics_json=audit_metrics,
        parse_repeatability_hash=evaluation.parse_repeatability_hash,
        e2e_repeatability_hash=evaluation.e2e_repeatability_hash,
        artifact_hash=evaluation.artifact_hash,
        enterprise_commit=evaluation.enterprise_commit,
        enterprise_worktree_dirty=bool(evaluation.enterprise_worktree_dirty),
        ragflow_source_tag=evaluation.ragflow_source_tag,
        ragflow_source_commit=evaluation.ragflow_source_commit,
        thresholds_version=evaluation.thresholds_version,
        thresholds_digest=evaluation.thresholds_digest,
    )

    # Reload mapping after potential concurrent changes
    doc = await gw_read(
        db, get_mapping, tenant_id, source_system, external_document_id, source_version_id,
    )
    if doc is None:
        raise ConfirmQualityError(404, "NOT_FOUND", "Document disappeared during confirm")

    try:
        promoted = await gw_write(
            db,
            promote_quality_passed_version,
            ragflow_client,
            doc,
            "passed",
        )
    except RAGFlowAPIError as exc:
        raise ConfirmQualityError(
            503,
            "RAGFLOW_UNAVAILABLE",
            f"RAGFlow unavailable during promote: {exc}",
            retryable=True,
        ) from exc
    except DocumentSyncError as exc:
        if isinstance(exc, RetryableDocumentSyncError) or getattr(exc, "retryable", False):
            raise ConfirmQualityError(
                503,
                "RAGFLOW_UNAVAILABLE",
                str(exc) or "RAGFlow unavailable",
                retryable=True,
            ) from exc
        raise ConfirmQualityError(409, "CONFLICT", str(exc) or exc.code) from exc

    if not promoted:
        raise ConfirmQualityError(
            409,
            "CONFLICT",
            "Promote refused (not latest or not eligible); no dual current created",
        )

    doc = await gw_read(
        db, get_mapping, tenant_id, source_system, external_document_id, source_version_id,
    )
    logger.info(
        "quality confirm applied tenant_id=%s external_document_id=%s "
        "source_version_id=%s decision_id=%s actor=%s current_version=%s",
        tenant_id,
        external_document_id,
        source_version_id,
        decision_id,
        actor,
        getattr(doc, "current_version", None),
    )
    return ConfirmQualityResult(
        external_document_id=external_document_id,
        source_version_id=source_version_id,
        parse_quality_status="passed",
        current_version=int(getattr(doc, "current_version", 0) or 0),
        business_status=getattr(doc, "business_status", "active"),
        promoted=True,
        idempotent=False,
        decision_id=decision_id,
        actor=actor,
        reason=reason,
    )
