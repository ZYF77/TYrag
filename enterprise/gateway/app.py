"""Enterprise Gateway FastAPI application - WP-02A Closure."""
import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from enterprise.gateway.sync.models import (
    ExtDocumentMap, init_db, insert_mapping, get_mapping,
    get_mapping_by_event_id, update_mapping_status,
)
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.middleware import UserAuthError, require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.models.ext_user_map import ExtUserMapRepo
from enterprise.gateway.sync.status_mapping import enterprise_stage, map_ragflow_run_to_sync_status
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentClient, RAGFlowAPIError,
)
from enterprise.gateway.sync.source_adapter import SourceAdapter

logger = logging.getLogger(__name__)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "metadata-schema.json"
with open(_SCHEMA_PATH) as f:
    METADATA_SCHEMA = json.load(f)

_db: aiosqlite.Connection | None = None


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
    yield
    if _db:
        await _db.close()
        _db = None


app = FastAPI(title="Enterprise RAGFlow Gateway", version="1.0.0", lifespan=lifespan)


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


# -- Pydantic models --

class SourceInfo(BaseModel):
    bucket: str
    objectKey: str


class DocumentUpsertRequest(BaseModel):
    eventId: str = Field(min_length=1)
    sourceSystem: str = Field(min_length=1, max_length=64)
    externalDocumentId: str = Field(min_length=1, max_length=128)
    sourceVersionId: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    fileName: str = Field(min_length=1, max_length=255)
    mediaType: str = Field(default="application/pdf")
    source: SourceInfo
    metadata: dict


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


# -- error codes --

ERROR_CODES = {
    "DOCUMENT_EVENT_DUPLICATE": (200, False),
    "DOCUMENT_METADATA_INVALID": (422, False),
    "DOCUMENT_HASH_MISMATCH": (422, False),
    "DOCUMENT_SOURCE_NOT_FOUND": (422, True),
    "DOCUMENT_SYNC_FAILED": (502, True),
    "RAGFLOW_UNAVAILABLE": (503, True),
    "RAGFLOW_API_INCOMPATIBLE": (503, False),
    "VALIDATION_ERROR": (422, False),
    "INTERNAL_ERROR": (500, False),
}


def error_response(code: str, message: str, request_id: str,
                   details: dict | None = None) -> JSONResponse:
    http_status, retryable = ERROR_CODES.get(code, (500, False))
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(
            code=code, message=message, requestId=request_id,
            retryable=retryable, details=details,
        ).model_dump()
    )


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
                         error_msg: str | None = None,
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
    if error_code:
        resp.error = ErrorResponse(
            code=error_code, message=error_msg or "",
            requestId=request_id,
        )
    return resp.model_dump()


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
        return error_response("DOCUMENT_HASH_MISMATCH", "Invalid SHA256 format", request_id)

    err = validate_metadata(req.metadata, request_id)
    if err:
        return error_response(err, "Metadata validation failed", request_id)

    existing = await get_mapping_by_event_id(db, req.eventId)
    if existing:
        return make_status_response(existing, deduplicated=True, request_id=request_id)

    tenant_id = req.metadata.get("tenant_id", "default")

    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        event_id=req.eventId,
        sha256=req.sha256,
        file_name=req.fileName,
        media_type=req.mediaType,
        sync_status="received",
    )

    try:
        doc = await insert_mapping(db, doc)
    except Exception as e:
        logger.error("Failed to insert mapping: %s", e)
        return error_response("INTERNAL_ERROR", str(e), request_id)

    if doc.sync_status != "received":
        existing2 = await get_mapping(
            db, tenant_id, req.sourceSystem,
            req.externalDocumentId, req.sourceVersionId)
        if existing2:
            return make_status_response(existing2, deduplicated=True, request_id=request_id)

    await update_mapping_status(db, doc, "validated")

    try:
        source_adapter = SourceAdapter()
        source_file = await source_adapter.fetch(
            req.source.bucket, req.source.objectKey, req.sha256)
    except Exception as e:
        await update_mapping_status(db, doc, "failed",
                                     error_code="DOCUMENT_SOURCE_NOT_FOUND",
                                     error_message=str(e))
        return error_response("DOCUMENT_SOURCE_NOT_FOUND",
                              f"Source file fetch failed: {e}", request_id)

    if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
        await update_mapping_status(db, doc, "registered")
        doc.sync_status = "registered"
        return make_status_response(doc, request_id=request_id)

    # Upload to RAGFlow with real status mapping
    try:
        client = RAGFlowDocumentClient(api_key=os.environ.get("RAGFLOW_API_KEY", "stub-key"))
        datasets = await client.list_datasets()
        dataset_id = None
        for ds in datasets:
            if ds.get("name") == f"enterprise-{tenant_id}":
                dataset_id = ds["id"]
                break
        if not dataset_id:
            result = await client.create_dataset(f"enterprise-{tenant_id}")
            dataset_id = result.get("data", {}).get("id", "")

        upload_result = await client.upload_document(
            dataset_id, req.fileName, source_file.content)
        docs_data = upload_result.get("data", [])
        if docs_data:
            ragflow_doc = docs_data[0]
            doc.ragflow_dataset_id = dataset_id
            doc.ragflow_document_id = ragflow_doc.get("id", "")
            doc.ragflow_task_id = ragflow_doc.get("id", "")

            # Store initial status from RAGFlow
            ragflow_run = ragflow_doc.get("run", "")
            mapped = map_ragflow_run_to_sync_status(ragflow_run)
            await update_mapping_status(db, doc, mapped,
                                         pipeline_status=ragflow_run)
        else:
            await update_mapping_status(db, doc, "failed",
                                         error_code="DOCUMENT_SYNC_FAILED",
                                         error_message="RAGFlow returned empty document data")
            return error_response("DOCUMENT_SYNC_FAILED",
                                  "RAGFlow returned empty document data", request_id)

    except RAGFlowAPIError as e:
        await update_mapping_status(db, doc, "failed",
                                     error_code="DOCUMENT_SYNC_FAILED",
                                     error_message=str(e))
        return error_response("DOCUMENT_SYNC_FAILED", str(e), request_id)

    return make_status_response(doc, request_id=request_id)


@app.get("/enterprise/api/v1/documents/{external_document_id}/status")
async def get_document_status(
    external_document_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())

    tenant_id = request.query_params.get("tenant_id", "default")
    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")

    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE external_document_id=? AND tenant_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (external_document_id, tenant_id)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    code="DOCUMENT_NOT_FOUND",
                    message=f"Document {external_document_id} not found",
                    requestId=request_id,
                ).model_dump()
            )

    doc = ExtDocumentMap(
        id=row["id"], tenant_id=row["tenant_id"], source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        event_id=row["event_id"], sha256=row["sha256"],
        file_name=row["file_name"], media_type=row["media_type"],
        ragflow_dataset_id=row["ragflow_dataset_id"],
        ragflow_document_id=row["ragflow_document_id"],
        ragflow_task_id=row["ragflow_task_id"],
        sync_status=row["sync_status"], pipeline_status=row["pipeline_status"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        last_sync_at=row["last_sync_at"],
        source_updated_at=row["source_updated_at"]
    )

    # Optional refresh from RAGFlow
    if refresh and doc.ragflow_dataset_id and doc.ragflow_document_id:
        try:
            client = RAGFlowDocumentClient(
                api_key=os.environ.get("RAGFLOW_API_KEY", "stub-key"))
            rf_docs = await client.list_documents(doc.ragflow_dataset_id)
            for rf in rf_docs:
                if rf.get("id") == doc.ragflow_document_id:
                    ragflow_run = rf.get("run", "")
                    if ragflow_run:
                        mapped = map_ragflow_run_to_sync_status(ragflow_run)
                        current = doc.sync_status
                        # Never downgrade a terminal success
                        if current == "ready" and mapped != "ready":
                            pass
                        else:
                            await update_mapping_status(
                                db, doc, mapped,
                                pipeline_status=ragflow_run)
                            doc.sync_status = mapped
                            doc.pipeline_status = ragflow_run
                    break
        except RAGFlowAPIError:
            logger.warning("RAGFlow status refresh failed for %s", doc.ragflow_document_id)
        except Exception:
            logger.exception("Unexpected error refreshing RAGFlow status")

    return make_status_response(doc, request_id=request_id)


@app.get("/enterprise/api/v1/health")
async def health():
    return {"status": "healthy", "version": "1.0.0"}


# -- WP-01A: end-user auth --

@app.get("/enterprise/api/v1/auth/me")
async def auth_me(
    principal: UserPrincipal = Depends(require_user_principal),
):
    """Return authenticated end-user principal.

    Never returns raw token, RAGFlow API key, internal PKs, or credential material.
    """
    return JSONResponse(content=principal.to_safe_dict())
