"""Versioned document integration routes with external-only identifiers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.asset_registry import (
    AssetRegistryConflict,
    AssetRegistryError,
    AssetRegistryInvalid,
    AssetRegistryUnavailable,
    resolve_asset,
)
from enterprise.gateway.db import GatewayDatabase
from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.exceptions import PersistenceConflictError
from enterprise.gateway.sync.models import (
    DocumentEventReceipt,
    ExtDocumentMap,
    OutboxEvent,
    enqueue_outbox,
    get_document_event_receipt,
    get_mapping,
    get_mapping_by_event_id,
    get_outbox_by_event_id,
    insert_document_event_receipt,
    insert_mapping,
)
from enterprise.gateway.sync.status_mapping import enterprise_stage
from enterprise.gateway.sync.sync_service import DocumentSyncError


router = APIRouter(prefix="/enterprise/api/v2/documents", tags=["documents-v2"])
_document_write_locks = WeakKeyDictionary()

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "metadata-schema.json"
with _SCHEMA_PATH.open(encoding="utf-8") as schema_file:
    _METADATA_SCHEMA = json.load(schema_file)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInfo(_StrictModel):
    bucket: str = Field(min_length=1)
    objectKey: str = Field(min_length=1)


class DocumentUpsertRequest(_StrictModel):
    eventId: str = Field(min_length=1, max_length=128)
    eventType: Literal["upsert", "reindex"]
    tenantId: str = Field(min_length=1, max_length=64)
    sourceSystem: str = Field(min_length=1, max_length=64)
    externalDocumentId: str = Field(min_length=1, max_length=128)
    sourceVersionId: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    fileName: str = Field(min_length=1, max_length=255)
    mediaType: str = Field(min_length=1, max_length=128)
    source: SourceInfo
    metadata: dict
    batchId: str | None = Field(default=None, max_length=128)


async def get_db() -> GatewayDatabase:
    """Reuse the app dependency while keeping this router independently testable."""
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    return await dependency()


async def _gw_read(gateway: GatewayDatabase, fn, /, *args, **kwargs):
    async with gateway.transaction(write=False) as conn:
        return await fn(conn, *args, **kwargs)


async def _gw_write(gateway: GatewayDatabase, fn, /, *args, **kwargs):
    async with gateway.transaction(write=True) as conn:
        return await fn(conn, *args, **kwargs)


async def require_v2_service_principal(
    principal: ServicePrincipal = Depends(require_service_principal),
) -> ServicePrincipal:
    """Require HMAC credential identity for v2 while preserving test isolation."""
    auth_enabled = os.environ.get(
        "ENTERPRISE_SYNC_AUTH_ENABLED", "true"
    ).lower() == "true"
    if (
        principal.credential_id
        or not auth_enabled
        or os.environ.get("ENTERPRISE_TEST_MODE") == "1"
    ):
        return principal
    raise HTTPException(
        status_code=401,
        detail={
            "code": "AUTH_SIGNATURE_MISSING",
            "message": "HMAC signature authentication is required",
        },
    )


def _sync_service(gateway: GatewayDatabase):
    from enterprise.gateway import app as app_module

    return app_module._sync_service(gateway)


async def _document_write_lock(
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> asyncio.Lock:
    # PostgreSQL provides the cross-process serialization; this lock only
    # avoids duplicate work within one event loop for the same document key.
    del tenant_id, source_system, external_document_id, source_version_id
    loop = asyncio.get_running_loop()
    lock = _document_write_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _document_write_locks[loop] = lock
    return lock


async def _persist_mapping_and_outbox(
    gateway: GatewayDatabase,
    mapping: ExtDocumentMap,
    event: OutboxEvent,
) -> tuple[ExtDocumentMap | None, bool]:
    lock = await _document_write_lock(
        mapping.tenant_id,
        mapping.source_system,
        mapping.external_document_id,
        mapping.source_version_id,
    )
    async with lock:
        async with gateway.transaction(write=True) as conn:
            doc, inserted = await insert_mapping(
                conn, mapping, return_inserted=True,
            )
            if doc is None:
                return None, False
            if inserted or doc.event_id == event.event_id:
                if not await get_outbox_by_event_id(conn, event.event_id):
                    await enqueue_outbox(conn, event)
            return doc, inserted



def _error(status_code: int, code: str, request_id: str) -> JSONResponse:
    messages = {
        "ACL_DENIED": "Access denied",
        "EVENT_ID_CONFLICT": "Event id was already used for another payload",
        "DOCUMENT_METADATA_INVALID": "Metadata validation failed",
        "DOCUMENT_TOO_LARGE": "Document exceeds the maximum supported size",
        "DOCUMENT_NOT_FOUND": "Document not found",
        "DOCUMENT_NOT_READY": "Document is not ready",
        "DOCUMENT_REVIEW_REQUIRED": "Document quality review is required",
        "DOCUMENT_SOURCE_NOT_FOUND": "Source file could not be retrieved",
        "DOCUMENT_SYNC_FAILED": "Document synchronization failed",
        "DOCUMENT_PARSE_FAILED": "Document parsing failed",
        "DOCUMENT_VERSION_CONFLICT": "Document version already has different content",
        "RAGFLOW_API_INCOMPATIBLE": "RAGFlow API is not compatible with the gateway",
        "RAGFLOW_UNAVAILABLE": "RAGFlow service is temporarily unavailable",
        "ASSET_REGISTRY_UNAVAILABLE": "Asset Registry is temporarily unavailable",
        "CONVERSATION_CONTEXT_CONFLICT": "Equipment identifiers conflict",
        "CONVERSATION_CONTEXT_INVALID": "Equipment identifier was not found",
        "VALIDATION_ERROR": "Request validation failed",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": messages.get(code, "Request failed"),
            "requestId": request_id,
            "retryable": code in {
                "RAGFLOW_UNAVAILABLE",
                "ASSET_REGISTRY_UNAVAILABLE",
                "DOCUMENT_SOURCE_NOT_FOUND",
                "DOCUMENT_SYNC_FAILED",
                "DOCUMENT_PARSE_FAILED",
            },
        },
    )


def _sync_error(exc: DocumentSyncError, request_id: str) -> JSONResponse:
    external_code = {
        "PARSER_APPLICATION_MISMATCH": "DOCUMENT_REVIEW_REQUIRED",
        "PARSER_APPLICATION_UNVERIFIABLE": "DOCUMENT_REVIEW_REQUIRED",
        "QUALITY_EVALUATION_ENQUEUE_FAILED": "RAGFLOW_UNAVAILABLE",
    }.get(exc.code, exc.code)
    statuses = {
        "DOCUMENT_NOT_FOUND": 404,
        "DOCUMENT_NOT_READY": 409,
        "DOCUMENT_REVIEW_REQUIRED": 409,
        "DOCUMENT_SOURCE_NOT_FOUND": 422,
        "DOCUMENT_HASH_MISMATCH": 422,
        "DOCUMENT_TOO_LARGE": 413,
        "DOCUMENT_SYNC_FAILED": 502,
        "DOCUMENT_PARSE_FAILED": 422,
        "RAGFLOW_UNAVAILABLE": 503,
        "RAGFLOW_API_INCOMPATIBLE": 503,
    }
    return _error(statuses.get(external_code, 500), external_code, request_id)


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
        "status": doc.sync_status,
        "stage": enterprise_stage(doc.sync_status),
        "deduplicated": deduplicated,
        "businessStatus": doc.business_status,
        "currentVersion": bool(doc.current_version),
        "eventStatus": doc.event_status,
        "updatedAt": doc.updated_at,
    }


def _accepted(payload: dict) -> JSONResponse:
    return JSONResponse(status_code=202, content=payload)


def _normalized_ingestion_payload(req: DocumentUpsertRequest | dict) -> dict:
    payload = req.model_dump(mode="json") if isinstance(req, BaseModel) else dict(req)
    payload.pop("eventId", None)
    payload.pop("batchId", None)
    if isinstance(payload.get("sha256"), str):
        payload["sha256"] = payload["sha256"].lower()
    return payload


def _canonical_hash(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(updated_at: str, row_id: int) -> str:
    raw = json.dumps(
        {"updatedAt": updated_at, "id": row_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(urlsafe_b64decode(padded).decode("utf-8"))
    updated_at = payload["updatedAt"]
    row_id = payload["id"]
    if not isinstance(updated_at, str) or not isinstance(row_id, int):
        raise ValueError("Invalid cursor")
    return updated_at, row_id


def _receipt_matches(
    receipt: DocumentEventReceipt,
    payload_hash: str,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> bool:
    return (
        receipt.payload_hash == payload_hash
        and receipt.tenant_id == tenant_id
        and receipt.source_system == source_system
        and receipt.external_document_id == external_document_id
        and receipt.source_version_id == source_version_id
    )


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
    lock = await _document_write_lock(
        tenant_id, source_system, external_document_id, source_version_id
    )
    async with lock:
        async with gateway.transaction(write=True) as conn:
            return await insert_document_event_receipt(
                conn,
            DocumentEventReceipt(
                event_id=event_id,
                payload_hash=payload_hash,
                tenant_id=tenant_id,
                source_system=source_system,
                external_document_id=external_document_id,
                source_version_id=source_version_id,
                outcome_code=outcome_code,
            ),
        )


def _scope_allowed(
    principal: ServicePrincipal, tenant_id: str, source_system: str
) -> bool:
    if principal.allowed_bindings:
        return (tenant_id, source_system) in principal.allowed_bindings
    return principal.source_system in {"service", "anonymous", source_system}


def _metadata_scope(req: DocumentUpsertRequest) -> str | None:
    try:
        jsonschema.validate(req.metadata, _METADATA_SCHEMA)
    except jsonschema.ValidationError:
        return None
    expected = {
        "tenant_id": req.tenantId,
        "source_system": req.sourceSystem,
        "external_document_id": req.externalDocumentId,
        "document_version": req.sourceVersionId,
    }
    if any(req.metadata.get(key) != value for key, value in expected.items()):
        return None
    return req.tenantId


async def _seed_test_registry_fixture(
    gateway: GatewayDatabase, req: DocumentUpsertRequest, tenant_id: str
) -> None:
    """Populate only the explicit offline registry fixture used by tests."""
    if os.environ.get("ENTERPRISE_TEST_MODE") != "1":
        return
    if os.environ.get("ENTERPRISE_EAM_ASSET_RESOLVER_MODE", "").strip():
        return
    equipment_id = req.metadata.get("equipment_id")
    fixed_asset_no = req.metadata.get("fixed_asset_no")
    asset_id = req.metadata.get("asset_id") or fixed_asset_no or equipment_id
    if not equipment_id and not fixed_asset_no and not asset_id:
        return
    lock = await _document_write_lock(
        tenant_id,
        req.sourceSystem,
        req.externalDocumentId,
        req.sourceVersionId,
    )
    async with lock:
        async with gateway.transaction(write=True) as conn:
            await exec_sql(conn,
            """INSERT INTO ext_asset_registry
               (tenant_id, equipment_id, fixed_asset_no, asset_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tenant_id, equipment_id) DO UPDATE SET
                 fixed_asset_no=excluded.fixed_asset_no,
                 asset_id=excluded.asset_id""",
            (tenant_id, equipment_id or fixed_asset_no or asset_id, fixed_asset_no, asset_id),
            )


async def _mapping_for_scope(
    gateway: GatewayDatabase,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str | None,
) -> ExtDocumentMap | None:
    async with gateway.transaction(write=False) as conn:
        if source_version_id:
            return await get_mapping(
                conn, tenant_id, source_system, external_document_id, source_version_id
            )
        row = await fetchone(
            conn,
            """SELECT * FROM ext_document_map
               WHERE tenant_id=? AND source_system=? AND external_document_id=?
               ORDER BY current_version DESC, updated_at DESC LIMIT 1""",
            (tenant_id, source_system, external_document_id),
        )
        if not row:
            return None
        from enterprise.gateway.sync.models import row_to_mapping

        return row_to_mapping(row)


async def _replay_receipt(
    gateway: GatewayDatabase,
    receipt: DocumentEventReceipt,
    payload_hash: str,
    request_id: str,
) -> JSONResponse:
    if not _receipt_matches(
        receipt,
        payload_hash,
        receipt.tenant_id,
        receipt.source_system,
        receipt.external_document_id,
        receipt.source_version_id,
    ):
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if receipt.outcome_code == "DOCUMENT_VERSION_CONFLICT":
        return _error(409, "DOCUMENT_VERSION_CONFLICT", request_id)
    doc = await _gw_read(
        gateway,
        get_mapping,
        receipt.tenant_id,
        receipt.source_system,
        receipt.external_document_id,
        receipt.source_version_id,
    )
    if not doc:
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    operation_id = (
        receipt.event_id
        if receipt.outcome_code in {"accepted", "reindex_accepted"}
        else doc.event_id
    )
    return _accepted(
        _status_payload(doc, deduplicated=True, operation_id=operation_id)
    )


@router.post("")
async def upsert_document(
    req: DocumentUpsertRequest,
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    tenant_id = _metadata_scope(req)
    if not tenant_id:
        return _error(422, "DOCUMENT_METADATA_INVALID", request_id)
    if not _scope_allowed(principal, tenant_id, req.sourceSystem):
        return _error(403, "ACL_DENIED", request_id)

    normalized = _normalized_ingestion_payload(req)
    payload_hash = _canonical_hash(normalized)
    receipt = await _gw_read(gateway, get_document_event_receipt, req.eventId)
    if receipt:
        if not _receipt_matches(
            receipt,
            payload_hash,
            tenant_id,
            req.sourceSystem,
            req.externalDocumentId,
            req.sourceVersionId,
        ):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        return await _replay_receipt(gateway, receipt, payload_hash, request_id)

    legacy_mapping = await _gw_read(gateway, get_mapping_by_event_id, req.eventId)
    legacy_outbox = await _gw_read(gateway, get_outbox_by_event_id, req.eventId)
    if legacy_mapping or legacy_outbox:
        if not legacy_mapping or not legacy_outbox:
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        legacy_identity = (
            legacy_mapping.tenant_id,
            legacy_mapping.source_system,
            legacy_mapping.external_document_id,
            legacy_mapping.source_version_id,
        )
        if legacy_identity != (
            tenant_id,
            req.sourceSystem,
            req.externalDocumentId,
            req.sourceVersionId,
        ):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        try:
            legacy_hash = _canonical_hash(
                _normalized_ingestion_payload(json.loads(legacy_outbox.payload))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        if legacy_hash != payload_hash:
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        await _record_receipt(
            gateway,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=tenant_id,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code="accepted",
        )
        return _accepted(
            _status_payload(legacy_mapping, deduplicated=True)
        )

    existing = await _gw_read(
        gateway,
        get_mapping,
        tenant_id,
        req.sourceSystem,
        req.externalDocumentId,
        req.sourceVersionId,
    )
    if existing:
        if existing.sha256.lower() != req.sha256.lower():
            outcome = "DOCUMENT_VERSION_CONFLICT"
        elif req.eventType == "reindex":
            try:
                existing = await _sync_service(gateway).reindex_document(
                    tenant_id,
                    req.sourceSystem,
                    req.externalDocumentId,
                    req.sourceVersionId,
                )
            except DocumentSyncError as exc:
                return _sync_error(exc, request_id)
            outcome = "reindex_accepted"
        else:
            outcome = "deduplicated"
        stored = await _record_receipt(
            gateway,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=tenant_id,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code=outcome,
        )
        if not _receipt_matches(
            stored,
            payload_hash,
            tenant_id,
            req.sourceSystem,
            req.externalDocumentId,
            req.sourceVersionId,
        ):
            return _error(409, "EVENT_ID_CONFLICT", request_id)
        if outcome == "DOCUMENT_VERSION_CONFLICT":
            return _error(409, outcome, request_id)
        return _accepted(
            _status_payload(
                existing,
                deduplicated=outcome == "deduplicated",
                operation_id=req.eventId if outcome == "reindex_accepted" else None,
            )
        )

    if req.eventType == "reindex":
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)

    await _seed_test_registry_fixture(gateway, req, tenant_id)
    try:
        async with gateway.transaction(write=False) as conn:
            canonical_asset = await resolve_asset(
                conn,
            tenant_id=tenant_id,
            equipment_id=req.metadata.get("equipment_id"),
            fixed_asset_no=req.metadata.get("fixed_asset_no"),
            asset_id=req.metadata.get("asset_id"),
        )
    except AssetRegistryUnavailable:
        return _error(503, "ASSET_REGISTRY_UNAVAILABLE", request_id)
    except AssetRegistryConflict:
        return _error(409, "CONVERSATION_CONTEXT_CONFLICT", request_id)
    except AssetRegistryInvalid:
        return _error(422, "CONVERSATION_CONTEXT_INVALID", request_id)
    except AssetRegistryError:
        return _error(503, "ASSET_REGISTRY_UNAVAILABLE", request_id)

    payload_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    mapping = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        event_id=req.eventId,
        event_type=req.eventType,
        event_status="received",
        sha256=req.sha256.lower(),
        file_name=req.fileName,
        media_type=req.mediaType,
        document_type=req.metadata.get("document_type"),
        source_page_count=req.metadata.get("page_count"),
        bucket=req.source.bucket,
        object_key=req.source.objectKey,
        asset_id=canonical_asset.asset_id,
        equipment_id=canonical_asset.equipment_id,
        fixed_asset_no=canonical_asset.fixed_asset_no,
        department_id=req.metadata.get("department_id"),
        security_level=req.metadata.get("security_level"),
        allow_group_ids=json.dumps(
            req.metadata.get("allow_group_ids") or [], ensure_ascii=False
        ),
        deny_group_ids=json.dumps(
            req.metadata.get("deny_group_ids") or [], ensure_ascii=False
        ),
        batch_id=req.batchId,
        sync_status="received",
    )
    try:
        doc, _inserted = await _persist_mapping_and_outbox(
            gateway,
            mapping,
            OutboxEvent(
                event_id=req.eventId,
                event_type=req.eventType,
                tenant_id=tenant_id,
                source_system=req.sourceSystem,
                external_document_id=req.externalDocumentId,
                source_version_id=req.sourceVersionId,
                batch_id=req.batchId,
                payload=payload_json,
            ),
        )
        if doc is None:
            return _error(409, "EVENT_ID_CONFLICT", request_id)
    except PersistenceConflictError:
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    doc_identity = (
        doc.tenant_id,
        doc.source_system,
        doc.external_document_id,
        doc.source_version_id,
    )
    if doc_identity != (
        tenant_id,
        req.sourceSystem,
        req.externalDocumentId,
        req.sourceVersionId,
    ):
        return _error(409, "EVENT_ID_CONFLICT", request_id)
    if doc.event_id != req.eventId:
        outcome = (
            "deduplicated"
            if doc.sha256.lower() == req.sha256.lower()
            else "DOCUMENT_VERSION_CONFLICT"
        )
        await _record_receipt(
            gateway,
            event_id=req.eventId,
            payload_hash=payload_hash,
            tenant_id=tenant_id,
            source_system=req.sourceSystem,
            external_document_id=req.externalDocumentId,
            source_version_id=req.sourceVersionId,
            outcome_code=outcome,
        )
        if outcome == "DOCUMENT_VERSION_CONFLICT":
            return _error(409, outcome, request_id)
        return _accepted(_status_payload(doc, deduplicated=True))

    await _record_receipt(
        gateway,
        event_id=req.eventId,
        payload_hash=payload_hash,
        tenant_id=tenant_id,
        source_system=req.sourceSystem,
        external_document_id=req.externalDocumentId,
        source_version_id=req.sourceVersionId,
        outcome_code="accepted",
    )
    return _accepted(_status_payload(doc))


@router.get("/sync-status")
async def list_document_status(
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    clauses = ["tenant_id=?", "source_system=?"]
    params: list[object] = [tenant_id, source_system]
    if cursor:
        try:
            cursor_updated_at, cursor_id = _decode_cursor(cursor)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _error(422, "VALIDATION_ERROR", request_id)
        clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
        params.extend([cursor_updated_at, cursor_updated_at, cursor_id])
    params.append(limit + 1)
    async with gateway.transaction(write=False) as conn:
        rows = await fetchall(
            conn,
            f"""SELECT * FROM ext_document_map
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id DESC LIMIT ?""",
            params,
        )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    from enterprise.gateway.sync.models import row_to_mapping

    items = [_status_payload(row_to_mapping(row)) for row in page_rows]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}


@router.get("/{external_document_id}/status")
async def get_document_status(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    source_version_id: str | None = Query(
        default=None, alias="sourceVersionId", min_length=1, max_length=64
    ),
    refresh: bool = False,
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    doc = await _mapping_for_scope(
        gateway, tenant_id, source_system, external_document_id, source_version_id
    )
    if not doc:
        return _error(404, "DOCUMENT_NOT_FOUND", request_id)
    if refresh and doc.ragflow_dataset_id and doc.ragflow_document_id:
        try:
            doc = await _sync_service(gateway).refresh_status(doc)
        except DocumentSyncError as exc:
            return _sync_error(exc, request_id)
    return _status_payload(doc)


@router.post("/{external_document_id}/disable")
async def disable_document(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    try:
        await _sync_service(gateway).disable_document(
            tenant_id, source_system, external_document_id
        )
    except DocumentSyncError as exc:
        return _sync_error(exc, request_id)
    return _accepted(
        {
            "externalDocumentId": external_document_id,
            "status": "accepted",
        }
    )


@router.post("/{external_document_id}/restore")
async def restore_document(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    try:
        await _sync_service(gateway).restore_document(
            tenant_id, source_system, external_document_id
        )
    except DocumentSyncError as exc:
        return _sync_error(exc, request_id)
    return _accepted(
        {"externalDocumentId": external_document_id, "status": "accepted"}
    )


@router.delete("/{external_document_id}")
async def delete_document(
    external_document_id: str,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    source_system: str = Query(alias="sourceSystem", min_length=1, max_length=64),
    gateway: GatewayDatabase = Depends(get_db),
    principal: ServicePrincipal = Depends(require_v2_service_principal),
):
    request_id = str(uuid.uuid4())
    if not _scope_allowed(principal, tenant_id, source_system):
        return _error(403, "ACL_DENIED", request_id)
    try:
        await _sync_service(gateway).delete_document(
            tenant_id, source_system, external_document_id
        )
    except DocumentSyncError as exc:
        return _sync_error(exc, request_id)
    return _accepted(
        {
            "externalDocumentId": external_document_id,
            "status": "accepted",
        }
    )
