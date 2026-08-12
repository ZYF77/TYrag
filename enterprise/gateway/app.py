"""Enterprise Gateway FastAPI application - WP-02A Closure."""
import json
import logging
import os
import re
import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from enterprise.gateway.sync.models import (
    ExtDocumentMap, OutboxEvent, init_db, insert_mapping, get_mapping,
    get_mapping_by_event_id, list_mappings,
    enqueue_outbox, get_outbox_by_event_id, row_to_mapping,
    update_mapping_status,
)
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.middleware import UserAuthError, require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.models.ext_user_map import ExtUserMapRepo
from enterprise.gateway.sync.status_mapping import enterprise_stage
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentClient, RAGFlowDocumentStub,
)
from enterprise.gateway.sync.source_adapter import (
    S3SourceAdapter, SourceAdapter, SourceStub,
)
from enterprise.gateway.sync.external_source import FileShareSourceAdapter, router as external_source_router
from enterprise.gateway.sync.transient_attachment import (
    TransientAttachmentBodyLimitMiddleware,
    TransientAttachmentCleanupWorker,
    TransientAttachmentService,
    attachment_cleanup_interval_seconds,
    router as transient_attachment_router,
)
from enterprise.gateway.sync.sync_service import (
    DocumentSyncError, DocumentNotFoundError, SyncService,
)
from enterprise.gateway.sync.worker import OutboxWorker, StatusReconciler
from enterprise.gateway.quality.worker import (
    QualityEvaluationService,
    QualityEvaluationWorker,
    QualityReconciler,
)
from enterprise.gateway.quality.routing import parser_application_readback_match
from enterprise.gateway.config import config, require_ragflow_api_key

logger = logging.getLogger(__name__)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "metadata-schema.json"
with open(_SCHEMA_PATH) as f:
    METADATA_SCHEMA = json.load(f)

_db: aiosqlite.Connection | None = None
_background_tasks: list[asyncio.Task] = []


def _test_mode() -> bool:
    return os.environ.get("ENTERPRISE_TEST_MODE") == "1"


def _source_adapter() -> SourceAdapter:
    return SourceStub() if _test_mode() else S3SourceAdapter()


def _ragflow_client() -> RAGFlowDocumentClient:
    if _test_mode():
        return RAGFlowDocumentStub()
    return RAGFlowDocumentClient(api_key=require_ragflow_api_key())


def _sync_service(db: aiosqlite.Connection) -> SyncService:
    return SyncService(
        db,
        _source_adapter(),
        _ragflow_client(),
        FileShareSourceAdapter(),
    )


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        db_path = os.environ.get(
            "ENTERPRISE_SYNC_DB_PATH", "enterprise/ext_document_map.db")
        _db = await init_db(db_path)
    return _db


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    _db = await get_db()
    if not _test_mode():
        require_ragflow_api_key()
    started_tasks: list[asyncio.Task] = []
    if config.worker_enabled and not _test_mode():
        service = _sync_service(_db)
        worker_task = asyncio.create_task(
            OutboxWorker(service).run_forever(config.outbox_poll_seconds)
        )
        reconciler_task = asyncio.create_task(
            StatusReconciler(service).run_forever(config.reconcile_seconds)
        )
        attachment_cleanup_task = asyncio.create_task(
            TransientAttachmentCleanupWorker(
                TransientAttachmentService(_db)
            ).run_forever(attachment_cleanup_interval_seconds())
        )
        started_tasks.extend([worker_task, reconciler_task, attachment_cleanup_task])
        _background_tasks.extend(started_tasks)
    if config.quality_worker_enabled and not _test_mode():
        quality_service = QualityEvaluationService(
            _db,
            _ragflow_client(),
            max_attempts=config.quality_max_attempts,
        )
        quality_worker_task = asyncio.create_task(
            QualityEvaluationWorker(quality_service).run_forever(
                config.quality_poll_seconds
            )
        )
        quality_reconciler_task = asyncio.create_task(
            QualityReconciler(
                quality_service,
                running_timeout_seconds=config.quality_running_timeout_seconds,
            ).run_forever(config.quality_reconcile_seconds)
        )
        started_tasks.extend([quality_worker_task, quality_reconciler_task])
        _background_tasks.extend([quality_worker_task, quality_reconciler_task])
    try:
        yield
    finally:
        for task in started_tasks:
            task.cancel()
        for task in started_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in started_tasks:
            if task in _background_tasks:
                _background_tasks.remove(task)
        if _db:
            await _db.close()
            _db = None


app = FastAPI(title="Enterprise RAGFlow Gateway", version="1.0.0", lifespan=lifespan)
app.add_middleware(TransientAttachmentBodyLimitMiddleware)


@app.exception_handler(UserAuthError)
async def user_auth_error_handler(request: Request, exc: UserAuthError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "requestId": str(uuid.uuid4()),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    request_id = str(uuid.uuid4())
    logger.warning(
        "Request validation failed with %d errors", len(exc.errors())
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            code="VALIDATION_ERROR",
            message=safe_error_message("VALIDATION_ERROR"),
            requestId=request_id,
        ).model_dump(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = str(uuid.uuid4())
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        code = str(exc.detail["code"])
        message = str(
            exc.detail.get("message") or safe_error_message(code)
        )
    elif exc.status_code == 404:
        code = "REQUEST_FAILED"
        message = safe_error_message(code)
    else:
        code = "VALIDATION_ERROR" if exc.status_code < 500 else "INTERNAL_ERROR"
        message = safe_error_message(code)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            requestId=request_id,
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = str(uuid.uuid4())
    logger.exception(
        "Unhandled gateway error for %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            code="INTERNAL_ERROR",
            message=safe_error_message("INTERNAL_ERROR"),
            requestId=request_id,
        ).model_dump(),
    )


# -- Pydantic models --

class SourceInfo(BaseModel):
    bucket: str
    objectKey: str


class DocumentUpsertRequest(BaseModel):
    eventId: str = Field(min_length=1)
    eventType: str = Field(default="upsert", pattern="^(upsert|reindex)$")
    sourceSystem: str = Field(min_length=1, max_length=64)
    externalDocumentId: str = Field(min_length=1, max_length=128)
    sourceVersionId: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    fileName: str = Field(min_length=1, max_length=255)
    mediaType: str = Field(default="application/pdf")
    source: SourceInfo
    metadata: dict
    batchId: str | None = Field(default=None, max_length=128)


class ErrorResponse(BaseModel):
    code: str
    message: str
    requestId: str
    retryable: bool = False
    details: dict | None = None


class DocumentSyncResponse(BaseModel):
    externalDocumentId: str
    sourceVersionId: str
    ragflowDatasetId: str | None = None
    ragflowDocumentId: str | None = None
    status: str
    stage: str | None = None
    deduplicated: bool = False
    error: ErrorResponse | None = None
    businessStatus: str = "active"
    currentVersion: bool = False
    eventStatus: str = "received"
    updatedAt: str = ""


# -- error codes --

ERROR_CODES = {
    "DOCUMENT_EVENT_DUPLICATE": (202, False),
    "EVENT_ID_CONFLICT": (409, False),
    "DOCUMENT_VERSION_CONFLICT": (409, False),
    "DOCUMENT_METADATA_INVALID": (422, False),
    "DOCUMENT_HASH_MISMATCH": (422, False),
    "DOCUMENT_SOURCE_NOT_FOUND": (422, True),
    "DOCUMENT_NOT_FOUND": (404, False),
    "DOCUMENT_SYNC_FAILED": (502, True),
    "DOCUMENT_REVIEW_REQUIRED": (409, False),
    "DOCUMENT_QUALITY_FAILED": (409, False),
    "DOCUMENT_QUALITY_PENDING": (409, False),
    "RAGFLOW_UNAVAILABLE": (503, True),
    "RAGFLOW_API_INCOMPATIBLE": (503, False),
    "AUTH_REPLAY_STORE_UNAVAILABLE": (503, True),
    "ASSET_REGISTRY_UNAVAILABLE": (503, True),
    "CONVERSATION_CONTEXT_REQUIRED": (422, False),
    "CONVERSATION_CONTEXT_STALE": (409, False),
    "RUN_INTERRUPTED": (503, False),
    "CONVERSATION_UNAVAILABLE": (503, True),
    "CONVERSATION_CONTEXT_CONFLICT": (409, False),
    "CONVERSATION_CONTEXT_INVALID": (422, False),
    "CONVERSATION_ARCHIVED": (409, False),
    "CLIENT_MESSAGE_ID_CONFLICT": (409, False),
    "SUGGESTION_STALE": (409, False),
    "SUGGESTION_NOT_FOUND": (404, False),
    "CITATION_NOT_FOUND": (404, False),
    "DOCUMENT_NOT_READY": (409, False),
    "RAGFLOW_SCOPE_VIOLATION": (502, False),
    "NO_RELIABLE_EVIDENCE": (200, False),
    "VALIDATION_ERROR": (422, False),
    "INTERNAL_ERROR": (500, False),
    "REQUEST_FAILED": (404, False),
}

SAFE_ERROR_MESSAGES = {
    "AUTH_TOKEN_INVALID": "Invalid authentication token",
    "AUTH_TOKEN_MISSING": "Authentication token is required",
    "AUTH_USER_DISABLED": "User account is disabled",
    "AUTH_USER_MAPPING_MISSING": "User mapping not found",
    "ACL_DENIED": "Access denied",
    "DOCUMENT_EVENT_DUPLICATE": "Document event already processed",
    "EVENT_ID_CONFLICT": "Event id was already used with a different payload",
    "DOCUMENT_VERSION_CONFLICT": "Document version content conflicts with the accepted version",
    "DOCUMENT_METADATA_INVALID": "Metadata validation failed",
    "DOCUMENT_HASH_MISMATCH": "Invalid SHA256 format",
    "DOCUMENT_SOURCE_NOT_FOUND": "Source file could not be retrieved",
    "DOCUMENT_NOT_FOUND": "Document not found",
    "DOCUMENT_SYNC_FAILED": "Document synchronization failed",
    "DOCUMENT_REVIEW_REQUIRED": "Document quality review is required",
    "DOCUMENT_QUALITY_FAILED": "Document quality check failed",
    "DOCUMENT_QUALITY_PENDING": "Document quality evaluation is pending",
    "RAGFLOW_UNAVAILABLE": "RAGFlow service is temporarily unavailable",
    "RAGFLOW_API_INCOMPATIBLE": "RAGFlow API is not compatible with the gateway",
    "AUTH_REPLAY_STORE_UNAVAILABLE": "Replay protection store is unavailable",
    "ASSET_REGISTRY_UNAVAILABLE": "Asset Registry is temporarily unavailable",
    "CONVERSATION_UNAVAILABLE": "Conversation history is temporarily unavailable",
    "CONVERSATION_CONTEXT_CONFLICT": "Conversation context identifiers conflict",
    "CONVERSATION_CONTEXT_INVALID": "Conversation context is not recognized",
    "CONVERSATION_CONTEXT_REQUIRED": "A canonical equipment context is required before sending a message",
    "CONVERSATION_CONTEXT_STALE": "Conversation context no longer matches the Asset Registry",
    "CONVERSATION_ARCHIVED": "Conversation is archived",
    "CLIENT_MESSAGE_ID_CONFLICT": "Client message id conflicts with an earlier request",
    "SUGGESTION_STALE": "Suggestion context is stale",
    "SUGGESTION_NOT_FOUND": "Suggestion not found",
    "CITATION_NOT_FOUND": "Citation not found",
    "DOCUMENT_NOT_READY": "Document is not ready",
    "RAGFLOW_SCOPE_VIOLATION": "RAGFlow retrieval returned an out-of-scope document",
    "NO_RELIABLE_EVIDENCE": "No reliable evidence was returned",
    "VALIDATION_ERROR": "Request validation failed",
    "INTERNAL_ERROR": "Internal service error",
    "REQUEST_FAILED": "Request could not be completed",
    "RUN_INTERRUPTED": "Message run lease expired before completion",
}


def safe_error_message(code: str, fallback: str = "") -> str:
    return SAFE_ERROR_MESSAGES.get(code, fallback or "Request failed")


def error_response(code: str, request_id: str,
                   details: dict | None = None) -> JSONResponse:
    http_status, retryable = ERROR_CODES.get(code, (500, False))
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(
            code=code,
            message=safe_error_message(code),
            requestId=request_id,
            retryable=retryable, details=details,
        ).model_dump()
    )


def accepted_response(payload: dict) -> JSONResponse:
    return JSONResponse(status_code=202, content=payload)


def _parser_profile(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value.get("profile") if isinstance(value, dict) else None


# -- helpers --

def validate_metadata(metadata: dict, request_id: str) -> str | None:
    try:
        jsonschema.validate(metadata, METADATA_SCHEMA)
        return None
    except jsonschema.ValidationError as e:
        logger.warning("Metadata validation failed: %s", e.message)
        return "DOCUMENT_METADATA_INVALID"


def make_status_response(doc: ExtDocumentMap, deduplicated: bool = False,
                         error_code: str | None = None,
                         request_id: str = "") -> dict:
    resp = DocumentSyncResponse(
        externalDocumentId=doc.external_document_id,
        sourceVersionId=doc.source_version_id,
        ragflowDatasetId=doc.ragflow_dataset_id,
        ragflowDocumentId=doc.ragflow_document_id,
        status=doc.sync_status,
        stage=enterprise_stage(doc.sync_status),
        deduplicated=deduplicated,
    )
    state = doc.parser_application_status or "legacy_unverified"
    readback_match = parser_application_readback_match(doc)
    extra = {
        "businessStatus": doc.business_status,
        "currentVersion": bool(doc.current_version),
        "eventStatus": doc.event_status,
        "updatedAt": doc.updated_at,
        "parserApplication": {
            "state": state,
            "selectedProfile": doc.parser_profile,
            "configuredProfile": _parser_profile(doc.parser_configured_json),
            "executedProfile": _parser_profile(doc.parser_executed_json),
            "readbackMatch": readback_match,
            "reasonCode": (
                None
                if readback_match
                else (
                    f"PARSER_APPLICATION_{state.upper()}"
                    if state != "executed"
                    else "PARSER_APPLICATION_READBACK_MISMATCH"
                )
            ),
        },
    }
    resp = resp.model_dump()
    resp.update(extra)
    if error_code:
        resp["error"] = ErrorResponse(
            code=error_code,
            message=safe_error_message(error_code),
            requestId=request_id,
            retryable=ERROR_CODES.get(error_code, (500, False))[1],
        ).model_dump()
    return resp


def sync_error_response(exc: DocumentSyncError, request_id: str) -> JSONResponse:
    code = getattr(exc, "code", "INTERNAL_ERROR")
    http_status, _ = ERROR_CODES.get(code, (500, False))
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(
            code=code,
            message=str(exc) or safe_error_message(code),
            requestId=request_id,
            retryable=bool(getattr(exc, "retryable", False)),
        ).model_dump(),
    )


# -- routes --

@app.post("/enterprise/api/v1/documents", response_model=DocumentSyncResponse)
async def upsert_document(
    req: DocumentUpsertRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())

    if not SHA256_RE.match(req.sha256):
        return error_response("DOCUMENT_HASH_MISMATCH", request_id)

    err = validate_metadata(req.metadata, request_id)
    if err:
        return error_response(err, request_id)

    tenant_id = req.metadata.get("tenant_id", "default")
    existing_outbox = await get_outbox_by_event_id(db, req.eventId)
    existing_mapping = await get_mapping_by_event_id(db, req.eventId)
    if not existing_mapping:
        existing_mapping = await get_mapping(
            db, tenant_id, req.sourceSystem,
            req.externalDocumentId, req.sourceVersionId,
        )
    if existing_outbox and existing_mapping:
        return accepted_response(
            make_status_response(
                existing_mapping, deduplicated=True, request_id=request_id,
            )
        )

    payload = req.model_dump(mode="json")
    event = OutboxEvent(
        event_id=req.eventId,
        event_type=req.eventType,
        tenant_id=tenant_id,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        batch_id=req.batchId,
        payload=json.dumps(payload),
        max_attempts=config.outbox_max_attempts,
    )
    await enqueue_outbox(db, event)

    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        event_id=req.eventId,
        event_type=req.eventType,
        event_status="received",
        sha256=req.sha256,
        file_name=req.fileName,
        media_type=req.mediaType,
        document_type=req.metadata.get("document_type"),
        source_page_count=req.metadata.get("page_count"),
        asset_id=(
            req.metadata.get("asset_id")
            or req.metadata.get("fixed_asset_no")
            or req.metadata.get("equipment_id")
        ),
        equipment_id=req.metadata.get("equipment_id"),
        fixed_asset_no=req.metadata.get("fixed_asset_no"),
        department_id=req.metadata.get("department_id"),
        security_level=req.metadata.get("security_level"),
        allow_group_ids=json.dumps(
            req.metadata.get("allow_group_ids") or [],
            ensure_ascii=False,
        ),
        deny_group_ids=json.dumps(
            req.metadata.get("deny_group_ids") or [],
            ensure_ascii=False,
        ),
        bucket=req.source.bucket,
        object_key=req.source.objectKey,
        batch_id=req.batchId,
        sync_status="received",
    )

    try:
        doc = await insert_mapping(db, doc)
    except Exception:
        logger.exception("Failed to insert mapping")
        return error_response("INTERNAL_ERROR", request_id)

    if doc.event_id != req.eventId:
        return accepted_response(
            make_status_response(doc, deduplicated=True, request_id=request_id)
        )

    if _test_mode():
        await update_mapping_status(
            db, doc, "registered", event_status="completed",
        )
        doc.sync_status = "registered"

    return accepted_response(make_status_response(doc, request_id=request_id))


@app.get("/enterprise/api/v1/documents/{external_document_id}/status")
async def get_document_status(
    external_document_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())

    tenant_id = request.query_params.get("tenant_id", "default")
    source_system = request.query_params.get("source_system")
    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")

    if source_system:
        query = (
            """SELECT * FROM ext_document_map
               WHERE external_document_id=? AND tenant_id=? AND source_system=?
               ORDER BY updated_at DESC LIMIT 1"""
        )
        params = (external_document_id, tenant_id, source_system)
    else:
        query = (
            """SELECT * FROM ext_document_map
               WHERE external_document_id=? AND tenant_id=?
               ORDER BY updated_at DESC LIMIT 1"""
        )
        params = (external_document_id, tenant_id)
    async with db.execute(query, params) as cursor:
        row = await cursor.fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    code="DOCUMENT_NOT_FOUND",
                    message=safe_error_message("DOCUMENT_NOT_FOUND"),
                    requestId=request_id,
                ).model_dump()
            )

    doc = row_to_mapping(row)

    # Optional refresh from RAGFlow
    if refresh and doc.ragflow_dataset_id and doc.ragflow_document_id:
        try:
            doc = await _sync_service(db).refresh_status(doc)
        except DocumentSyncError:
            logger.warning("RAGFlow status refresh failed for %s", doc.ragflow_document_id)

    return make_status_response(doc, request_id=request_id)


@app.get("/enterprise/api/v1/documents/sync-status")
async def list_sync_status(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    tenant_id = request.query_params.get("tenant_id")
    source_system = request.query_params.get("source_system")
    status = request.query_params.get("status")
    batch_id = request.query_params.get("batch_id")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = 100, 0
    docs = await list_mappings(
        db,
        tenant_id=tenant_id,
        source_system=source_system,
        status=status,
        batch_id=batch_id,
        limit=limit,
        offset=offset,
    )
    return [
        {
            "externalDocumentId": doc.external_document_id,
            "sourceVersionId": doc.source_version_id,
            "fileName": doc.file_name,
            "status": doc.sync_status,
            "stage": enterprise_stage(doc.sync_status),
            "error": (
                ErrorResponse(
                    code=doc.last_error_code or "INTERNAL_ERROR",
                    message=doc.last_error_message or safe_error_message(
                        doc.last_error_code or "INTERNAL_ERROR"
                    ),
                    requestId=str(uuid.uuid4()),
                    retryable=ERROR_CODES.get(
                        doc.last_error_code or "INTERNAL_ERROR", (500, False)
                    )[1],
                ).model_dump()
                if doc.last_error_code
                else None
            ),
            "updatedAt": doc.updated_at,
            "businessStatus": doc.business_status,
            "currentVersion": bool(doc.current_version),
            "batchId": doc.batch_id,
        }
        for doc in docs
    ]


@app.post("/enterprise/api/v1/documents/{external_document_id}/disable")
async def disable_document(
    external_document_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    tenant_id = request.query_params.get("tenant_id", "default")
    source_system = request.query_params.get("source_system", "")
    if not source_system:
        return error_response("VALIDATION_ERROR", request_id)
    try:
        versions = await _sync_service(db).disable_document(
            tenant_id, source_system, external_document_id,
        )
    except DocumentSyncError as e:
        return sync_error_response(e, request_id)
    return accepted_response({
        "externalDocumentId": external_document_id,
        "status": "disabled",
        "updatedVersions": len(versions),
        "requestId": request_id,
    })


@app.post("/enterprise/api/v1/documents/{external_document_id}/restore")
async def restore_document(
    external_document_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    tenant_id = request.query_params.get("tenant_id", "default")
    source_system = request.query_params.get("source_system", "")
    if not source_system:
        return error_response("VALIDATION_ERROR", request_id)
    try:
        doc = await _sync_service(db).restore_document(
            tenant_id, source_system, external_document_id,
        )
    except DocumentSyncError as e:
        return sync_error_response(e, request_id)
    return accepted_response(make_status_response(doc, request_id=request_id))


@app.delete("/enterprise/api/v1/documents/{external_document_id}")
async def delete_document(
    external_document_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    tenant_id = request.query_params.get("tenant_id", "default")
    source_system = request.query_params.get("source_system", "")
    if not source_system:
        return error_response("VALIDATION_ERROR", request_id)
    try:
        versions = await _sync_service(db).delete_document(
            tenant_id, source_system, external_document_id,
        )
    except DocumentSyncError as e:
        return sync_error_response(e, request_id)
    return JSONResponse(
        status_code=202,
        content={
            "externalDocumentId": external_document_id,
            "status": "deleted",
            "deletedVersions": len(versions),
            "requestId": request_id,
        },
    )


@app.get("/enterprise/api/v1/health")
async def health():
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/")
async def root():
    return {
        "status": "healthy",
        "service": "Enterprise RAGFlow Gateway",
        "docs": "/docs",
        "health": "/enterprise/api/v1/health",
    }


# -- WP-01A: end-user auth --

@app.get("/enterprise/api/v1/auth/me")
async def auth_me(
    principal: UserPrincipal = Depends(require_user_principal),
):
    """Return authenticated end-user principal.

    Never returns raw token, RAGFlow API key, internal PKs, or credential material.
    """
    return JSONResponse(content=principal.to_safe_dict())


# Temporary query demo router, owned by the WP-04 retrieval scope
from enterprise.gateway.query.router import router as query_router
if config.demo_routes_enabled:
    app.include_router(query_router)

# Formal WP-04 query, conversation, SSE and citation API
from enterprise.gateway.query.formal_router import router as formal_query_router
app.include_router(formal_query_router)

# Frozen external v2 conversation API. It keeps v1 wire compatibility intact.
from enterprise.gateway.query.v2_router import router as v2_query_router
app.include_router(v2_query_router)

# Frozen external v2 document API; v1 routes above remain wire compatible.
from enterprise.gateway.sync.v2_router import router as v2_document_router
app.include_router(v2_document_router)
from enterprise.gateway.sync.v3_router import router as v3_document_router
app.include_router(v3_document_router)
app.include_router(external_source_router)
app.include_router(transient_attachment_router)

# WP-03 Phase 2 quality status APIs
from enterprise.gateway.quality.router import router as quality_router
app.include_router(quality_router)


@app.api_route(
    "/enterprise/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def enterprise_not_found(full_path: str):
    request_id = str(uuid.uuid4())
    return JSONResponse(
        status_code=404,
        content=ErrorResponse(
            code="REQUEST_FAILED",
            message=safe_error_message("REQUEST_FAILED"),
            requestId=request_id,
        ).model_dump(),
    )
