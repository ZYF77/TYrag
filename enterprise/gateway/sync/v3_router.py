"""v3 FILE_SHARE document ingestion.

v2 remains the S3 contract.  v3 deliberately accepts only a read-only file
share coordinate and hands RAGFlow a short-lived opaque source ticket.
"""

from __future__ import annotations

import json
import inspect
import sqlite3
import uuid
from typing import Literal

import aiosqlite
import jsonschema
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from enterprise.gateway.asset_registry import (
    AssetRegistryConflict,
    AssetRegistryError,
    AssetRegistryInvalid,
    AssetRegistryUnavailable,
    resolve_asset,
)
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.sync.document_catalog import validate_document_classification
from enterprise.gateway.sync.models import (
    DocumentEventReceipt,
    ExtDocumentMap,
    OutboxEvent,
    get_document_event_receipt,
    get_mapping,
    get_mapping_by_event_id,
    get_outbox_by_event_id,
    insert_mapping,
)
from enterprise.gateway.sync.status_mapping import enterprise_stage
from enterprise.gateway.sync.sync_service import DocumentSyncError
from enterprise.gateway.sync import v2_router as v2


router = APIRouter(prefix="/enterprise/api/v3/documents", tags=["documents-v3"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileShareSource(_StrictModel):
    kind: Literal["FILE_SHARE"]
    storageRootId: str = Field(min_length=1, max_length=64)
    relativePath: str = Field(min_length=1, max_length=1024)
    size: int | None = Field(default=None, ge=0)
    etag: str | None = Field(default=None, max_length=256)


class DocumentUpsertRequest(_StrictModel):
    eventId: str = Field(min_length=1, max_length=128)
    eventType: Literal["upsert", "reindex"]
    tenantId: str = Field(min_length=1, max_length=64)
    sourceSystem: str = Field(min_length=1, max_length=64)
    externalDocumentId: str = Field(min_length=1, max_length=128)
    sourceVersionId: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    fileName: str = Field(min_length=1, max_length=255)
    mediaType: Literal["application/pdf"]
    source: FileShareSource
    metadata: dict
    batchId: str | None = Field(default=None, max_length=128)


async def get_db() -> aiosqlite.Connection:
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value


async def require_v3_service_principal(
    principal: ServicePrincipal = Depends(v2.require_v2_service_principal),
) -> ServicePrincipal:
    return principal


def _error(status_code: int, code: str, request_id: str) -> JSONResponse:
    messages = {
        "ACL_DENIED": "Access denied",
        "DOCUMENT_METADATA_INVALID": "Metadata validation failed",
        "DOCUMENT_NOT_FOUND": "Document not found",
        "DOCUMENT_VERSION_CONFLICT": "Document version already has different content",
        "DOCUMENT_SOURCE_NOT_FOUND": "Document source is unavailable",
        "VALIDATION_ERROR": "Request validation failed",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": messages.get(code, "Request failed"),
            "requestId": request_id,
            "retryable": code in {"DOCUMENT_SOURCE_NOT_FOUND"},
        },
    )


def _status_payload(
    doc: ExtDocumentMap,
    *,
    deduplicated: bool = False,
    operation_id: str | None = None,
) -> dict:
    return {
        "operationId": operation_id or doc.event_id,
        "externalDocumentId": doc.external_document_id,
        "sourceVersionId": doc.source_version_id,
        "sourceKind": doc.source_kind,
        "status": doc.sync_status,
        "stage": enterprise_stage(doc.sync_status),
        "ingestState": doc.ingest_state,
        "sourceState": doc.source_state,
        "deduplicated": deduplicated,
        "businessStatus": doc.business_status,
        "currentVersion": bool(doc.current_version),
        "eventStatus": doc.event_status,
        "updatedAt": doc.updated_at,
    }


def _normalized_request(req: DocumentUpsertRequest) -> tuple[dict, dict]:
    metadata = dict(req.metadata)
    for snake, camel in (
        ("document_subtype", "documentSubtype"),
        ("source_document_type", "sourceDocumentType"),
    ):
        if snake not in metadata and camel in metadata:
            metadata[snake] = metadata[camel]
        metadata.pop(camel, None)
    document_type, subtype, source_document_type = validate_document_classification(metadata)
    metadata["document_type"] = document_type
    if subtype is not None:
        metadata["document_subtype"] = subtype
    if source_document_type is not None:
        metadata["source_document_type"] = source_document_type

    schema_metadata = dict(metadata)
    schema_metadata.pop("document_subtype", None)
    schema_metadata.pop("source_document_type", None)
    try:
        jsonschema.validate(schema_metadata, v2._METADATA_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError("metadata does not satisfy the v1 identity contract") from exc
    expected = {
        "tenant_id": req.tenantId,
        "source_system": req.sourceSystem,
        "external_document_id": req.externalDocumentId,
        "document_version": req.sourceVersionId,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError("metadata identity does not match request")

    payload = req.model_dump(mode="json")
    payload["metadata"] = metadata
    payload.pop("eventId", None)
    payload.pop("batchId", None)
    payload["sha256"] = payload["sha256"].lower()
    return payload, metadata


def _scope_allowed(principal: ServicePrincipal, tenant_id: str, source_system: str) -> bool:
    return v2._scope_allowed(principal, tenant_id, source_system)


async def _record_receipt(
    db: aiosqlite.Connection,
    *,
    event_id: str,
    payload_hash: str,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    outcome_code: str,
) -> DocumentEventReceipt:
    return await v2._record_receipt(
        db,
        event_id=event_id,
        payload_hash=payload_hash,
        tenant_id=tenant_id,
        source_system=source_system,
        external_document_id=external_document_id,
        source_version_id=source_version_id,
        outcome_code=outcome_code,
    )


def _sync_service(db: aiosqlite.Connection):
    from enterprise.gateway import app as app_module

    return app_module._sync_service(db)


def _file_name_is_safe(file_name: str) -> bool:
    return file_name not in {".", ".."} and "/" not in file_name and "\\" not in file_name


def _source_version_matches(
    doc: ExtDocumentMap, req: DocumentUpsertRequest, metadata: dict
) -> bool:
    source = req.source
    return (
        doc.source_kind == "FILE_SHARE"
        and doc.sha256.lower() == req.sha256.lower()
        and doc.storage_root_id == source.storageRootId
        and doc.relative_path == source.relativePath
        and doc.document_type == metadata.get("document_type")
        and doc.document_subtype == metadata.get("document_subtype")
        and doc.source_document_type == metadata.get("source_document_type")
        and (source.size is None or doc.source_size is None or doc.source_size == source.size)
        and (source.etag is None or doc.source_etag is None or doc.source_etag == source.etag)
    )


@router.post("")
async def upsert_document(
    req: DocumentUpsertRequest,
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _file_name_is_safe(req.fileName):
        return _error(422, "VALIDATION_ERROR", request_id)
    if not _scope_allowed(principal, req.tenantId, req.sourceSystem):
        return _error(403, "ACL_DENIED", request_id)
    try:
        normalized, metadata = _normalized_request(req)
    except ValueError:
        return _error(422, "DOCUMENT_METADATA_INVALID", request_id)

    payload_hash = v2._canonical_hash(normalized)
    receipt = await get_document_event_receipt(db, req.eventId)
    if receipt:
        if not v2._receipt_matches(
            receipt, payload_hash, req.tenantId, req.sourceSystem,
            req.externalDocumentId, req.sourceVersionId,
        ):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        return await _replay_receipt(db, receipt, payload_hash, request_id)

    legacy_mapping = await get_mapping_by_event_id(db, req.eventId)
    legacy_outbox = await get_outbox_by_event_id(db, req.eventId)
    if legacy_mapping or legacy_outbox:
        return _error(409, "EVENT_ID_CONFLICT", request_id)

    existing = await get_mapping(
        db, req.tenantId, req.sourceSystem,
        req.externalDocumentId, req.sourceVersionId,
    )
    if existing:
        if not _source_version_matches(existing, req, metadata):
            outcome = "DOCUMENT_VERSION_CONFLICT"
        elif req.eventType == "reindex":
            try:
                existing = await _sync_service(db).reindex_document(
                    req.tenantId, req.sourceSystem,
                    req.externalDocumentId, req.sourceVersionId,
                )
            except DocumentSyncError as exc:
                return _sync_error(exc, request_id)
            outcome = "reindex_accepted"
        else:
            outcome = "deduplicated"
        await _record_receipt(
            db,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=req.tenantId,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code=outcome,
        )
        if outcome == "DOCUMENT_VERSION_CONFLICT":
            return _error(409, outcome, request_id)
        return JSONResponse(
            status_code=202,
            content=_status_payload(
                existing,
                deduplicated=outcome == "deduplicated",
                operation_id=req.eventId if outcome == "reindex_accepted" else None,
            ),
        )

    if req.eventType == "reindex":
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)

    await v2._seed_test_registry_fixture(db, req, req.tenantId)
    try:
        canonical_asset = await resolve_asset(
            db,
            tenant_id=req.tenantId,
            equipment_id=metadata.get("equipment_id"),
            fixed_asset_no=metadata.get("fixed_asset_no"),
            asset_id=metadata.get("asset_id"),
        )
    except AssetRegistryUnavailable:
        return _error(503, "ASSET_REGISTRY_UNAVAILABLE", request_id)
    except AssetRegistryConflict:
        return _error(409, "CONVERSATION_CONTEXT_CONFLICT", request_id)
    except AssetRegistryInvalid:
        return _error(422, "CONVERSATION_CONTEXT_INVALID", request_id)
    except AssetRegistryError:
        return _error(503, "ASSET_REGISTRY_UNAVAILABLE", request_id)

    mapping = ExtDocumentMap(
        tenant_id=req.tenantId,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        event_id=req.eventId,
        event_type=req.eventType,
        event_status="received",
        sha256=req.sha256.lower(),
        file_name=req.fileName,
        media_type=req.mediaType,
        document_type=metadata["document_type"],
        source_page_count=metadata.get("page_count"),
        source_kind="FILE_SHARE",
        storage_root_id=req.source.storageRootId,
        relative_path=req.source.relativePath,
        source_size=req.source.size,
        source_etag=req.source.etag,
        asset_id=canonical_asset.asset_id,
        equipment_id=canonical_asset.equipment_id,
        fixed_asset_no=canonical_asset.fixed_asset_no,
        department_id=metadata.get("department_id"),
        security_level=metadata.get("security_level"),
        allow_group_ids=json.dumps(metadata.get("allow_group_ids") or [], ensure_ascii=False),
        deny_group_ids=json.dumps(metadata.get("deny_group_ids") or [], ensure_ascii=False),
        document_subtype=metadata.get("document_subtype"),
        source_document_type=metadata.get("source_document_type"),
        ingest_state="RECEIVED",
        source_state="AVAILABLE",
        batch_id=req.batchId,
        sync_status="received",
    )
    try:
        doc, _inserted = await v2._persist_mapping_and_outbox(
            db,
            mapping,
            OutboxEvent(
                event_id=req.eventId,
                event_type=req.eventType,
                tenant_id=req.tenantId,
                source_system=req.sourceSystem,
                external_document_id=req.externalDocumentId,
                source_version_id=req.sourceVersionId,
                batch_id=req.batchId,
                payload=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
            ),
        )
    except sqlite3.IntegrityError:
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if doc is None:
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if doc.event_id != req.eventId:
        if doc.sha256.lower() != req.sha256.lower():
            return _error(409, "DOCUMENT_VERSION_CONFLICT", request_id)
        await _record_receipt(
            db,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=req.tenantId,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code="deduplicated",
        )
        return JSONResponse(status_code=202, content=_status_payload(doc, deduplicated=True))

    await _record_receipt(
        db,
        event_id=req.eventId,
        payload_hash=payload_hash,
        tenant_id=req.tenantId,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        outcome_code="accepted",
    )
    return JSONResponse(status_code=202, content=_status_payload(doc))


async def _replay_receipt(
    db: aiosqlite.Connection,
    receipt: DocumentEventReceipt,
    payload_hash: str,
    request_id: str,
):
    if not v2._receipt_matches(
        receipt, payload_hash, receipt.tenant_id, receipt.source_system,
        receipt.external_document_id, receipt.source_version_id,
    ):
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if receipt.outcome_code == "DOCUMENT_VERSION_CONFLICT":
        return _error(409, "DOCUMENT_VERSION_CONFLICT", request_id)
    doc = await get_mapping(
        db, receipt.tenant_id, receipt.source_system,
        receipt.external_document_id, receipt.source_version_id,
    )
    if not doc:
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    return JSONResponse(
        status_code=202,
        content=_status_payload(
            doc,
            deduplicated=True,
            operation_id=receipt.event_id,
        ),
    )


def _sync_error(exc: DocumentSyncError, request_id: str) -> JSONResponse:
    statuses = {
        "DOCUMENT_NOT_FOUND": 404,
        "DOCUMENT_NOT_READY": 409,
        "DOCUMENT_SOURCE_NOT_FOUND": 422,
        "DOCUMENT_HASH_MISMATCH": 422,
        "RAGFLOW_UNAVAILABLE": 503,
    }
    return _error(statuses.get(exc.code, 500), exc.code, request_id)


@router.get("/sync-status")
async def list_document_status(
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND source_kind='FILE_SHARE'
           ORDER BY updated_at DESC, id DESC LIMIT ?""",
        (tenant_id, source_system, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    from enterprise.gateway.sync.models import row_to_mapping

    return {"items": [_status_payload(row_to_mapping(row)) for row in rows]}


@router.get("/{external_document_id}/status")
async def get_document_status(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    source_version_id: str | None = Query(default=None, alias="sourceVersionId"),
    db: aiosqlite.Connection = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    doc = await v2._mapping_for_scope(
        db, tenant_id, source_system, external_document_id, source_version_id,
    )
    if not doc or doc.source_kind != "FILE_SHARE":
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    return _status_payload(doc)
