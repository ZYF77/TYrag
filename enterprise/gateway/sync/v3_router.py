"""v3 FILE_SHARE and INLINE_JSON document ingestion."""

from __future__ import annotations

import json
import inspect
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

import jsonschema
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.db import GatewayDatabase, PersistenceConflictError
from enterprise.gateway.db.dialect import fetchall
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
from enterprise.gateway.sync.readiness import (
    DocumentCandidateReadiness,
    document_candidate_readiness,
    document_candidate_readiness_from_db,
)
from enterprise.gateway.sync.sync_service import (
    DocumentSyncError,
    inline_json_bytes_and_sha256,
)
from enterprise.gateway.sync import v2_router as v2


router = APIRouter(prefix="/enterprise/api/v3/documents", tags=["documents-v3"])
MAX_DOCUMENT_FEED_BODY_BYTES = 2 * 1024 * 1024
MAX_INLINE_JSON_DEPTH = 20
_FORBIDDEN_INLINE_KEYS = {
    "password", "passwd", "pwd", "token", "apitoken", "accesstoken",
    "refreshtoken",
    "secret", "apikey", "authorization", "cookie", "privatekey",
    "clientsecret", "base64", "filecontent", "binary", "attachmentcontent",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileShareSource(_StrictModel):
    kind: Literal["FILE_SHARE"]
    storageRootId: str = Field(min_length=1, max_length=64)
    relativePath: str = Field(min_length=1, max_length=1024)
    size: int | None = Field(default=None, ge=0)
    etag: str | None = Field(default=None, max_length=256)


class InlineJsonSource(_StrictModel):
    kind: Literal["INLINE_JSON"]
    content: dict[str, Any]


class DocumentUpsertRequest(_StrictModel):
    eventId: str = Field(min_length=1, max_length=128)
    eventType: Literal["upsert", "reindex"]
    tenantId: str = Field(min_length=1, max_length=64)
    sourceSystem: str = Field(min_length=1, max_length=64)
    externalDocumentId: str = Field(min_length=1, max_length=128)
    sourceVersionId: str = Field(min_length=1, max_length=64)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    fileName: str = Field(min_length=1, max_length=255)
    mediaType: Literal["application/pdf", "application/json"]
    source: Annotated[FileShareSource | InlineJsonSource, Field(discriminator="kind")]
    metadata: dict
    batchId: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_source_contract(self):
        if isinstance(self.source, FileShareSource):
            if self.sha256 is None or self.mediaType != "application/pdf":
                raise ValueError("FILE_SHARE requires sha256 and application/pdf")
        elif (
            self.sha256 is not None
            or self.mediaType != "application/json"
            or not self.fileName.lower().endswith(".json")
        ):
            raise ValueError(
                "INLINE_JSON forbids sha256 and requires application/json with .json fileName"
            )
        return self


class InlineJsonContentError(ValueError):
    pass


def _normalized_key(value: str) -> str:
    return value.replace("_", "").replace("-", "").lower()


def _validate_inline_content(content: dict[str, Any]) -> list[Any]:
    equipment_values: list[Any] = []
    pending: list[tuple[Any, int]] = [(content, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_INLINE_JSON_DEPTH:
            raise InlineJsonContentError("INLINE_JSON exceeds maximum nesting depth")
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = _normalized_key(key)
                if normalized in _FORBIDDEN_INLINE_KEYS:
                    raise InlineJsonContentError("INLINE_JSON contains a forbidden field")
                if normalized == "equipmentid":
                    equipment_values.append(child)
                if isinstance(child, (dict, list)):
                    pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend(
                (child, depth + 1)
                for child in value
                if isinstance(child, (dict, list))
            )
    return equipment_values



async def _gw_read(gateway: GatewayDatabase, fn, /, *args, **kwargs):
    async with gateway.transaction(write=False) as conn:
        return await fn(conn, *args, **kwargs)


async def get_db() -> GatewayDatabase:
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value


async def require_v3_service_principal(
    principal: ServicePrincipal = Depends(v2.require_v2_service_principal),
) -> ServicePrincipal:
    return principal


def _error(
    status_code: int,
    code: str,
    request_id: str,
    *,
    retryable: bool | None = None,
) -> JSONResponse:
    from enterprise.gateway.app import safe_error_message
    from enterprise.gateway.app import ERROR_CODES

    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": safe_error_message(code),
            "requestId": request_id,
            "retryable": (
                ERROR_CODES.get(code, (status_code, False))[1]
                if retryable is None
                else retryable
            ),
        },
    )


_STATUS_ERROR_MESSAGES = {
    "DOCUMENT_SOURCE_NOT_FOUND": "找不到源文件。",
    "DOCUMENT_HASH_MISMATCH": "文件校验值与登记内容不一致。",
    "DOCUMENT_PARSE_FAILED": "文档解析失败。",
    "PARSER_APPLICATION_MISMATCH": "文档解析配置校验未通过。",
    "DOCUMENT_SYNC_FAILED": "文档同步失败，请稍后重试。",
    "RAGFLOW_UNAVAILABLE": "文档处理服务暂时不可用，请稍后重试。",
    "INTERNAL_ERROR": "服务开小差了，请稍后重试。",
}
def _status_url(
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> str:
    path_id = quote(external_document_id, safe="")
    query = "&".join(
        f"{key}={quote(value, safe='')}"
        for key, value in (
            ("tenantId", tenant_id),
            ("sourceSystem", source_system),
            ("sourceVersionId", source_version_id),
        )
    )
    return f"/enterprise/api/v3/documents/{path_id}/status?{query}"


def _status_error(doc: ExtDocumentMap) -> dict | None:
    code = doc.last_error_code
    if not code and doc.sync_status == "failed":
        code = "DOCUMENT_SYNC_FAILED"
    if not code:
        return None
    safe_code = code if code in _STATUS_ERROR_MESSAGES else "DOCUMENT_SYNC_FAILED"
    retryable = bool(doc.last_error_retryable)
    if safe_code != code:
        retryable = True
    return {
        "code": safe_code,
        "message": _STATUS_ERROR_MESSAGES[safe_code],
        "retryable": retryable,
    }


def _accept_payload(
    doc: ExtDocumentMap,
    *,
    deduplicated: bool = False,
    operation_id: str | None = None,
) -> dict:
    """Slim registration acceptance receipt (FILE_SHARE 3.1.0)."""
    return {
        "operationId": operation_id or doc.event_id,
        "externalDocumentId": doc.external_document_id,
        "sourceVersionId": doc.source_version_id,
        "deduplicated": deduplicated,
        "updatedAt": doc.updated_at,
    }


def _status_payload(
    doc: ExtDocumentMap,
    *,
    deduplicated: bool = False,
    operation_id: str | None = None,
    readiness: DocumentCandidateReadiness | None = None,
    quality_status: str | None = None,
) -> dict:
    readiness = readiness or document_candidate_readiness(
        doc, quality_allowed=False, quality_required=True,
    )
    pipeline_status = str(doc.pipeline_status) if doc.pipeline_status is not None else None
    index_completed = bool(
        pipeline_status and pipeline_status.upper() in {"DONE", "3"}
    )
    error = _status_error(doc)
    return {
        "operationId": operation_id or doc.event_id,
        "externalDocumentId": doc.external_document_id,
        "sourceVersionId": doc.source_version_id,
        "statusUrl": _status_url(
            doc.tenant_id,
            doc.source_system,
            doc.external_document_id,
            doc.source_version_id,
        ),
        "sourceKind": doc.source_kind,
        "status": doc.sync_status,
        "stage": enterprise_stage(doc.sync_status),
        "pipelineStatus": pipeline_status,
        "parseCompleted": readiness.parser_readback,
        "indexCompleted": index_completed,
        "ingestState": doc.ingest_state,
        "sourceState": doc.source_state,
        "deduplicated": deduplicated,
        "businessStatus": doc.business_status,
        "currentVersion": bool(doc.current_version),
        "eventStatus": doc.event_status,
        "updatedAt": doc.updated_at,
        "retrievable": readiness.retrievable,
        "readiness": {
            "currentVersion": readiness.current_version,
            "active": readiness.active,
            "syncReady": readiness.sync_ready,
            "parserReadback": readiness.parser_readback,
            "ragflowIdsPresent": readiness.ragflow_ids_present,
            "qualityPassed": readiness.quality_allowed,
            "blockingReason": readiness.blocking_reason,
        },
        "qualityStatus": quality_status,
        "errorCode": error["code"] if error else None,
        "error": error,
    }


async def _status_payload_for_db(
    gateway: GatewayDatabase,
    doc: ExtDocumentMap,
    *,
    deduplicated: bool = False,
    operation_id: str | None = None,
) -> dict:
    async with gateway.transaction(write=False) as conn:
        readiness, quality_status = await document_candidate_readiness_from_db(conn, doc)
    return _status_payload(
        doc,
        deduplicated=deduplicated,
        operation_id=operation_id,
        readiness=readiness,
        quality_status=quality_status,
    )


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
    if isinstance(req.source, InlineJsonSource):
        equipment_values = _validate_inline_content(req.source.content)
        if any(value != metadata["equipment_id"] for value in equipment_values):
            raise ValueError("INLINE_JSON equipment_id conflicts with metadata")
        _, digest = inline_json_bytes_and_sha256(req.source.content)
        payload["sha256"] = digest
    else:
        payload["sha256"] = str(payload["sha256"]).lower()
    return payload, metadata


def _scope_allowed(principal: ServicePrincipal, tenant_id: str, source_system: str) -> bool:
    return v2._scope_allowed(principal, tenant_id, source_system)


async def _record_receipt(
    gateway: GatewayDatabase,
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
        gateway,
        event_id=event_id,
        payload_hash=payload_hash,
        tenant_id=tenant_id,
        source_system=source_system,
        external_document_id=external_document_id,
        source_version_id=source_version_id,
        outcome_code=outcome_code,
    )


def _sync_service(gateway: GatewayDatabase):
    from enterprise.gateway import app as app_module

    return app_module._sync_service(gateway)


def _file_name_is_safe(file_name: str) -> bool:
    return file_name not in {".", ".."} and "/" not in file_name and "\\" not in file_name


def _source_version_matches(
    doc: ExtDocumentMap, req: DocumentUpsertRequest, metadata: dict
) -> bool:
    source = req.source
    if isinstance(source, InlineJsonSource):
        _, digest = inline_json_bytes_and_sha256(source.content)
        return (
            doc.source_kind == "INLINE_JSON"
            and doc.sha256.lower() == digest
            and doc.equipment_id == metadata.get("equipment_id")
            and doc.document_type == metadata.get("document_type")
            and doc.document_subtype == metadata.get("document_subtype")
            and doc.source_document_type == metadata.get("source_document_type")
        )
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
    request: Request,
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if len(await request.body()) > MAX_DOCUMENT_FEED_BODY_BYTES:
        return _error(422, "VALIDATION_ERROR", request_id)
    if not _file_name_is_safe(req.fileName):
        return _error(422, "VALIDATION_ERROR", request_id)
    if not _scope_allowed(principal, req.tenantId, req.sourceSystem):
        return _error(403, "ACL_DENIED", request_id)
    try:
        normalized, metadata = _normalized_request(req)
    except InlineJsonContentError:
        return _error(422, "VALIDATION_ERROR", request_id)
    except ValueError:
        return _error(422, "DOCUMENT_METADATA_INVALID", request_id)

    payload_hash = v2._canonical_hash(normalized)
    receipt = await _gw_read(gateway, get_document_event_receipt, req.eventId)
    if receipt:
        if not v2._receipt_matches(
            receipt, payload_hash, req.tenantId, req.sourceSystem,
            req.externalDocumentId, req.sourceVersionId,
        ):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        return await _replay_receipt(gateway, receipt, payload_hash, request_id)

    legacy_mapping = await _gw_read(gateway, get_mapping_by_event_id, req.eventId)
    legacy_outbox = await _gw_read(gateway, get_outbox_by_event_id, req.eventId)
    if legacy_mapping or legacy_outbox:
        return _error(409, "EVENT_ID_CONFLICT", request_id)

    existing = await _gw_read(gateway, get_mapping, req.tenantId, req.sourceSystem,
        req.externalDocumentId, req.sourceVersionId,
    )
    if existing:
        if not _source_version_matches(existing, req, metadata):
            outcome = "DOCUMENT_VERSION_CONFLICT"
        elif req.eventType == "reindex":
            try:
                existing = await _sync_service(gateway).reindex_document(
                    req.tenantId, req.sourceSystem,
                    req.externalDocumentId, req.sourceVersionId,
                )
            except DocumentSyncError as exc:
                return _sync_error(exc, request_id)
            outcome = "reindex_accepted"
        else:
            existing, requeued = await _sync_service(gateway).ensure_present_or_requeue(
                existing,
            )
            outcome = "accepted" if requeued else "deduplicated"
        await _record_receipt(
            gateway,
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
            content=_accept_payload(
                existing,
                deduplicated=outcome == "deduplicated",
                operation_id=req.eventId if outcome != "deduplicated" else None,
            ),
        )

    if req.eventType == "reindex":
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)

    source_kind = req.source.kind
    internal_sha256 = normalized["sha256"]
    mapping = ExtDocumentMap(
        tenant_id=req.tenantId,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        event_id=req.eventId,
        event_type=req.eventType,
        event_status="received",
        sha256=internal_sha256,
        file_name=req.fileName,
        media_type=req.mediaType,
        document_type=metadata["document_type"],
        source_page_count=metadata.get("page_count"),
        source_kind=source_kind,
        storage_root_id=(
            req.source.storageRootId if isinstance(req.source, FileShareSource) else None
        ),
        relative_path=(
            req.source.relativePath if isinstance(req.source, FileShareSource) else None
        ),
        source_size=(
            req.source.size
            if isinstance(req.source, FileShareSource)
            else len(inline_json_bytes_and_sha256(req.source.content)[0])
        ),
        source_etag=req.source.etag if isinstance(req.source, FileShareSource) else None,
        asset_id=metadata.get("asset_id"),
        equipment_id=metadata["equipment_id"],
        fixed_asset_no=metadata.get("fixed_asset_no"),
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
            gateway,
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
    except PersistenceConflictError:
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if doc is None:
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if doc.event_id != req.eventId:
        if doc.sha256.lower() != internal_sha256:
            return _error(409, "DOCUMENT_VERSION_CONFLICT", request_id)
        await _record_receipt(
            gateway,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=req.tenantId,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code="deduplicated",
        )
        return JSONResponse(
            status_code=202,
            content=_accept_payload(doc, deduplicated=True),
        )

    await _record_receipt(
        gateway,
        event_id=req.eventId,
        payload_hash=payload_hash,
        tenant_id=req.tenantId,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        outcome_code="accepted",
    )
    return JSONResponse(
        status_code=202,
        content=_accept_payload(doc),
    )


async def _replay_receipt(
    gateway: GatewayDatabase,
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
    doc = await _gw_read(gateway, get_mapping, receipt.tenant_id, receipt.source_system,
        receipt.external_document_id, receipt.source_version_id,
    )
    if not doc:
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    requeued = False
    if receipt.outcome_code != "reindex_accepted":
        doc, requeued = await _sync_service(gateway).ensure_present_or_requeue(doc)
    return JSONResponse(
        status_code=202,
        content=_accept_payload(
            doc,
            deduplicated=not requeued,
            operation_id=receipt.event_id,
        ),
    )


def _sync_error(exc: DocumentSyncError, request_id: str) -> JSONResponse:
    statuses = {
        "DOCUMENT_NOT_FOUND": 404,
        "DOCUMENT_NOT_READY": 409,
        "DOCUMENT_SOURCE_NOT_FOUND": 422,
        "DOCUMENT_HASH_MISMATCH": 422,
        "DOCUMENT_PARSE_FAILED": 422,
        "DOCUMENT_SYNC_FAILED": 502,
        "RAGFLOW_UNAVAILABLE": 503,
    }
    return _error(
        statuses.get(exc.code, 500),
        exc.code,
        request_id,
        retryable=exc.retryable,
    )


@router.get("/sync-status")
async def list_document_status(
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    async with gateway.transaction(write=False) as conn:
        rows = await fetchall(
            conn,
            """SELECT * FROM ext_document_map
               WHERE tenant_id=? AND source_system=?
                 AND source_kind IN ('FILE_SHARE', 'INLINE_JSON')
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (tenant_id, source_system, limit),
        )
    from enterprise.gateway.sync.models import row_to_mapping

    items = []
    for row in rows:
        items.append(await _status_payload_for_db(gateway, row_to_mapping(row)))
    return {"items": items}


@router.get("/{external_document_id:path}/status")
async def get_document_status(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    source_version_id: str | None = Query(default=None, alias="sourceVersionId"),
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v3_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    doc = await v2._mapping_for_scope(
        gateway, tenant_id, source_system, external_document_id, source_version_id,
    )
    if not doc or doc.source_kind not in {"FILE_SHARE", "INLINE_JSON"}:
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    return await _status_payload_for_db(gateway, doc)
