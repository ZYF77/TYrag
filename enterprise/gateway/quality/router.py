"""Quality status APIs protected by end-user ACL or service auth."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.quality import models as quality_models
from enterprise.gateway.quality.gate import quality_dimensions, safe_metric_summary
from enterprise.gateway.quality.routing import route_document
from enterprise.gateway.query import acl_store
from enterprise.gateway.sync.models import get_mapping, get_versions_for_document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/documents", tags=["quality"])


async def get_db():
    from enterprise.gateway import app as app_module

    dep = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    return await dep()


def _error(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    from enterprise.gateway.app import ErrorResponse

    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            requestId=request_id,
        ).model_dump(),
    )


async def _authorized_document(
    db,
    principal: UserPrincipal,
    external_document_id: str,
    source_system: str,
    source_version_id: str | None,
    request_id: str,
):
    if source_version_id:
        doc = await get_mapping(
            db,
            principal.tenant_id,
            source_system,
            external_document_id,
            source_version_id,
        )
    else:
        versions = await get_versions_for_document(
            db, principal.tenant_id, source_system, external_document_id,
        )
        doc = max(
            versions,
            key=lambda v: (v.current_version, v.updated_at or ""),
            default=None,
        )
    if not doc:
        return None, None
    await acl_store.ensure_schema(db)
    allowed = await acl_store.is_allowed(
        db,
        tenant_id=principal.tenant_id,
        external_document_id=external_document_id,
        business_user_id=principal.business_user_id,
    )
    if not allowed:
        return None, _error(403, "ACL_DENIED", "Access denied", request_id)
    return doc, None


def _quality_response(evaluation) -> dict:
    metrics = evaluation.metrics_json if evaluation else {}
    return {
        "externalDocumentId": evaluation.external_document_id if evaluation else None,
        "sourceVersionId": evaluation.source_version_id if evaluation else None,
        "evaluationState": evaluation.evaluation_state if evaluation else "not_started",
        "evaluationVersion": evaluation.evaluation_version if evaluation else None,
        "parseQualityStatus": evaluation.parse_quality_status if evaluation else None,
        "qualityReasons": evaluation.quality_reasons if evaluation else [],
        "parserProfile": evaluation.parser_profile if evaluation else None,
        "policyVersion": evaluation.routing_policy_version if evaluation else None,
        "completedAt": evaluation.completed_at if evaluation else None,
        "metricSummary": safe_metric_summary(metrics),
        "qualityDimensions": quality_dimensions(metrics),
    }


@router.get("/quality-status")
async def list_quality_status(
    request: Request,
    db=Depends(get_db),
    principal=Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = 100, 0
    items = await quality_models.list_evaluations(
        db,
        tenant_id=request.query_params.get("tenant_id"),
        source_system=request.query_params.get("source_system"),
        status=request.query_params.get("status"),
        parser_profile=request.query_params.get("parser_profile"),
        batch_id=request.query_params.get("batch_id"),
        limit=limit,
        offset=offset,
    )
    return [_quality_response(item) for item in items]


@router.get("/{external_document_id}/quality")
async def get_document_quality(
    external_document_id: str,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_user_principal),
):
    request_id = str(uuid.uuid4())
    source_system = request.query_params.get("source_system", "DEMO")
    source_version_id = request.query_params.get("source_version_id")
    doc, acl_error = await _authorized_document(
        db,
        principal,
        external_document_id,
        source_system,
        source_version_id,
        request_id,
    )
    if acl_error:
        return acl_error
    if doc is None:
        return _error(404, "DOCUMENT_NOT_FOUND", "Document not found", request_id)
    evaluation = await quality_models.get_latest_evaluation(
        db,
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )
    return _quality_response(evaluation)


@router.post("/{external_document_id}/quality:reevaluate")
async def reevaluate_document_quality(
    external_document_id: str,
    request: Request,
    db=Depends(get_db),
    principal=Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    source_system = request.query_params.get("source_system", "")
    source_version_id = request.query_params.get("source_version_id", "")
    if not source_system or not source_version_id:
        return _error(
            422, "VALIDATION_ERROR", "source_system and source_version_id required",
            request_id,
        )
    tenant_id = request.query_params.get("tenant_id", "default")
    doc = await get_mapping(
        db, tenant_id, source_system, external_document_id, source_version_id,
    )
    if doc is None:
        return _error(404, "DOCUMENT_NOT_FOUND", "Document not found", request_id)
    routing = route_document(
        media_type=doc.media_type,
        file_name=doc.file_name,
        source_system=doc.source_system,
    )
    version = await quality_models.next_evaluation_version(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=doc.ragflow_dataset_id,
        ragflow_document_id=doc.ragflow_document_id,
        routing=routing,
        evaluation_version=version,
    )
    return JSONResponse(
        status_code=202,
        content={
            "externalDocumentId": doc.external_document_id,
            "sourceVersionId": doc.source_version_id,
            "evaluationId": evaluation.id,
            "evaluationVersion": evaluation.evaluation_version,
            "requestId": request_id,
        },
    )
