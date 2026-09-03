"""Internal system-admin management APIs for the gateway settings page.

These endpoints are NOT part of the external OpenAPI contract: every route
sets ``include_in_schema=False`` and requires the ``admin`` capability
(mapped from the ``system_admin`` role).  The runtime-settings endpoint is
the only configuration write; the EAM reachability probe performs a
server-side GET against the configured origin's JWKS path and never writes to
the database.

Responses never contain HMAC secrets, API keys, credential material, file
hashes, or storage paths.  The "no message bodies" convention applies to the
metadata LIST endpoints only: the conversation-session endpoint
(``GET /metadata/conversations/{id}/messages``) intentionally echoes the
user/assistant message text for the admin session-management page and returns
safe citation display metadata.  It never returns reasoning text, attachments,
temporary download URLs, or credentials.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.callback_delivery import CallbackEndpoint, parse_callback_endpoints
from enterprise.gateway.config import config
from enterprise.gateway.db.dialect import fetchall, fetchone
from enterprise.gateway.query.attachment_context import MAX_MESSAGE_FILES
from enterprise.gateway.runtime_settings import (
    ATTACHMENT_MAX_MIB,
    DOCUMENT_MAX_MIB,
    POLL_MAX_SECONDS,
    POLL_MIN_SECONDS,
    QUALITY_TIMEOUT_MAX_SECONDS,
    QUALITY_TIMEOUT_MIN_SECONDS,
    RuntimeSettingsError,
    TTL_MAX_SECONDS,
    TTL_MIN_SECONDS,
    parse_runtime_settings,
)
from enterprise.gateway.sync.models import utc_now
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError
from enterprise.gateway.sync.transient_attachment import attachment_max_size_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/admin/system", tags=["system-admin"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200
_MAX_MESSAGE_ITEMS = 500
_PROBE_PATH = "/.well-known/jwks.json"
_PROBE_TIMEOUT_SECONDS = 5.0

_ADMIN_CITATION_FIELDS = (
    "citationId",
    "sourceType",
    "title",
    "externalDocumentId",
    "sourceVersionId",
    "pageNo",
    "assetId",
    "recordType",
    "recordId",
    "refIndex",
    "fileKind",
    "excerpt",
)


def _admin_citations(value: object) -> list[dict[str, object]]:
    """Project stored citations for the admin viewer without issuing tickets."""
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        result.append({key: item[key] for key in _ADMIN_CITATION_FIELDS if key in item})
    return result


async def get_db():
    """Reuse the app gateway dependency while keeping this router testable."""
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value


class _RuntimeSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeWorkerSettings(_RuntimeSettingsModel):
    enabled: StrictBool
    pollSeconds: float = Field(ge=POLL_MIN_SECONDS, le=POLL_MAX_SECONDS)


class RuntimeCleanupSettings(RuntimeWorkerSettings):
    ttlSeconds: StrictInt = Field(ge=TTL_MIN_SECONDS, le=TTL_MAX_SECONDS)


class RuntimeQualityReconcilerSettings(RuntimeWorkerSettings):
    runningTimeoutSeconds: StrictInt = Field(
        ge=QUALITY_TIMEOUT_MIN_SECONDS,
        le=QUALITY_TIMEOUT_MAX_SECONDS,
    )


class RuntimeLimitsSettings(_RuntimeSettingsModel):
    fileShareMaxMiB: StrictInt = Field(ge=1, le=DOCUMENT_MAX_MIB)
    s3MaxMiB: StrictInt = Field(ge=1, le=DOCUMENT_MAX_MIB)
    transientAttachmentMaxMiB: StrictInt = Field(ge=1, le=ATTACHMENT_MAX_MIB)


class RuntimeDiagnosticsSettings(_RuntimeSettingsModel):
    enabled: StrictBool


class RuntimeSettingsRequest(_RuntimeSettingsModel):
    outbox: RuntimeWorkerSettings
    statusReconciler: RuntimeWorkerSettings
    transientAttachmentCleanup: RuntimeCleanupSettings
    qualityEvaluation: RuntimeWorkerSettings
    qualityReconciler: RuntimeQualityReconcilerSettings
    callbackDelivery: RuntimeWorkerSettings
    limits: RuntimeLimitsSettings
    diagnostics: RuntimeDiagnosticsSettings


async def _runtime_manager(gateway):
    from enterprise.gateway import app as app_module

    manager = app_module.runtime_settings_manager_for(gateway)
    await manager.ensure_loaded()
    return manager


def _error(status_code: int, code: str, request_id: str) -> JSONResponse:
    from enterprise.gateway.app import ErrorResponse, safe_error_message

    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=safe_error_message(code),
            requestId=request_id,
        ).model_dump(),
    )


def _pagination(request: Request) -> tuple[int, int]:
    try:
        limit = int(request.query_params.get("limit", str(_DEFAULT_LIMIT)))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        limit, offset = _DEFAULT_LIMIT, 0
    return max(1, min(limit, _MAX_LIMIT)), max(0, offset)


def _resolve_ordering(
    request: Request, columns: dict[str, str], default_key: str
) -> tuple[str, str]:
    """Resolve orderBy/order query params against a fixed column whitelist.

    非法或缺失的排序参数一律回退默认，不报错。列名只能取自白名单映射、
    方向只接受 asc/desc（大小写不敏感），用户输入永远不会拼接进 SQL。
    """
    order_by = (request.query_params.get("orderBy") or "").strip()
    column = columns.get(order_by, columns[default_key])
    direction = (request.query_params.get("order") or "").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "desc"
    return column, direction.upper()


def _split_url(url: str) -> tuple[str, str]:
    """Split a callback URL into origin (baseUrl) and path (+query)."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return base_url, path


def _visible_callbacks(principal: UserPrincipal) -> dict[str, CallbackEndpoint]:
    """Parse ENTERPRISE_CALLBACK_ENDPOINTS and keep only tenant-visible bindings.

    Global bindings (no ``|`` separator) are visible to every tenant.
    Tenant-scoped bindings (``tenant|sourceSystem``) are visible only to that
    tenant, determined by the prefix before the first ``|``.
    """
    raw = os.environ.get("ENTERPRISE_CALLBACK_ENDPOINTS")
    try:
        endpoints = parse_callback_endpoints(
            raw, default_secret=config.callback_hmac_secret
        )
    except (ValueError, json.JSONDecodeError):
        # Malformed server configuration must never surface its contents.
        return {}
    tenant_id = principal.tenant_id
    return {
        binding: endpoint
        for binding, endpoint in endpoints.items()
        if "|" not in binding or binding.split("|", 1)[0] == tenant_id
    }


def _callback_item(binding: str, endpoint: CallbackEndpoint) -> dict:
    tenant_part, separator, source_part = binding.partition("|")
    base_url, path = _split_url(endpoint.url)
    return {
        "binding": binding,
        "tenantId": tenant_part if separator else None,
        "sourceSystem": source_part if separator else binding,
        "baseUrl": base_url,
        "path": path,
        "method": "POST",
        "enabled": bool(config.runtime_settings().callback_enabled),
        "credentialConfigured": bool(endpoint.secret or endpoint.key_id),
    }


@router.get("/integrations", include_in_schema=False)
async def list_integrations(
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Show the RAGFlow connection card and tenant-visible callback bindings."""
    runtime = await _runtime_manager(gateway)
    settings = runtime.snapshot()
    api_prefix = f"/api/{config.ragflow_api_version}"
    callbacks = _visible_callbacks(principal)
    return {
        "ragflow": {
            "baseUrl": config.ragflow_base_url,
            "apiVersion": config.ragflow_api_version,
            "paths": {
                "health": f"{api_prefix}/system/ping",
                "datasets": f"{api_prefix}/datasets",
                "chats": f"{api_prefix}/chats",
                "completions": f"{api_prefix}/chat/completions",
                "retrieval": f"{api_prefix}/retrieval",
            },
            "processing": {
                "maxConcurrentTasks": config.ragflow_max_concurrent_tasks,
                "maxConcurrentChunkBuilders": config.ragflow_max_concurrent_chunk_builders,
                "executorWorkers": config.ragflow_executor_workers,
            },
        },
        "limits": {
            "fileShareMaxBytes": settings.file_share_max_size_mb * 1024 * 1024,
            "s3MaxBytes": settings.s3_max_size_mb * 1024 * 1024,
            "transientAttachmentMaxBytes": attachment_max_size_bytes(),
            "transientAttachmentMaxFiles": MAX_MESSAGE_FILES,
        },
        "gatewayProcessing": {
            "outboxInFlight": 1,
            "qualityInFlight": 1,
            "callbackBatch": 10,
            "callbackConcurrent": 1,
        },
        "runtime": runtime.response(),
        "callbacksEnabled": bool(settings.callback_enabled),
        "callbacks": [
            _callback_item(binding, callbacks[binding]) for binding in sorted(callbacks)
        ],
    }


@router.put("/runtime-settings", include_in_schema=False)
async def update_runtime_settings(
    payload: RuntimeSettingsRequest,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Persist allow-listed Gateway settings and publish them immediately."""
    try:
        settings = parse_runtime_settings(payload.model_dump(mode="json"))
    except RuntimeSettingsError:
        return _error(422, "VALIDATION_ERROR", str(uuid.uuid4()))
    manager = await _runtime_manager(gateway)
    await manager.update(settings, updated_by=principal.subject)
    return manager.response()


class EamProbeRequest(BaseModel):
    binding: str = Field(min_length=1)


def _build_probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(_PROBE_TIMEOUT_SECONDS),
    )


async def _probe_jwks(url: str) -> dict:
    """GET the JWKS endpoint and classify the outcome without leaking the body."""
    started = time.monotonic()
    status = "failed"
    http_status: int | None = None
    error_code: str | None = None
    client = _build_probe_client()
    try:
        response = await client.get(url)
        http_status = response.status_code
        if 200 <= response.status_code < 300:
            try:
                payload = response.json()
            except ValueError:
                error_code = "PROBE_INVALID_RESPONSE"
            else:
                if isinstance(payload, dict) and isinstance(payload.get("keys"), list):
                    status = "connected"
                else:
                    error_code = "PROBE_INVALID_RESPONSE"
        elif 300 <= response.status_code < 400:
            error_code = "PROBE_REDIRECTED"
        else:
            error_code = "PROBE_HTTP_ERROR"
    except httpx.TimeoutException:
        error_code = "PROBE_TIMEOUT"
    except httpx.RequestError:
        error_code = "PROBE_CONNECT_FAILED"
    finally:
        await client.aclose()
    result: dict = {
        "status": status,
        "httpStatus": http_status,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "checkedAt": utc_now(),
    }
    if error_code is not None:
        result["errorCode"] = error_code
    return result


@router.post("/eam-probe", include_in_schema=False)
async def probe_eam(
    payload: EamProbeRequest,
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Probe the configured origin of a known callback binding via its JWKS.

    Only bindings returned by ``/integrations`` are accepted — never arbitrary
    URLs.  The probe is a server-side GET of ``{origin}/.well-known/jwks.json``
    with a 5 second timeout and redirects disabled.  It never calls the
    callback itself and never writes to the database.
    """
    request_id = str(uuid.uuid4())
    binding = payload.binding.strip()
    if not binding:
        return _error(422, "VALIDATION_ERROR", request_id)
    endpoint = _visible_callbacks(principal).get(binding)
    if endpoint is None:
        return _error(404, "PROBE_TARGET_NOT_FOUND", request_id)
    origin = _split_url(endpoint.url)[0]
    probe_url = f"{origin}{_PROBE_PATH}"
    result = await _probe_jwks(probe_url)
    logger.info(
        "eam probe binding=%s status=%s http_status=%s latency_ms=%s",
        binding,
        result["status"],
        result.get("httpStatus"),
        result.get("latencyMs"),
    )
    return {
        "binding": binding,
        "probeUrl": probe_url,
        **result,
    }


_CONVERSATION_ITEM_KEYS = (
    "conversation_id",
    "business_user_id",
    "equipment_id",
    "fixed_asset_no",
    "status",
    "ragflow_chat_id",
    "ragflow_session_id",
    "context_version",
    "created_at",
    "last_message_at",
)

_CONVERSATION_ORDER_COLUMNS = {
    "conversationId": "conversation_id",
    "businessUserId": "business_user_id",
    "equipmentId": "equipment_id",
    "fixedAssetNo": "fixed_asset_no",
    "status": "status",
    "contextVersion": "context_version",
    "createdAt": "created_at",
    "lastMessageAt": "last_message_at",
}
_CONVERSATION_FILTER_COLUMNS = {
    "conversationId": "conversation_id",
    "businessUserId": "business_user_id",
    "equipmentId": "equipment_id",
    "fixedAssetNo": "fixed_asset_no",
}
_DEFAULT_CONVERSATION_ORDER_BY = "lastMessageAt"
_CONVERSATION_TIEBREAKER = "conversation_id DESC"


@router.get("/metadata/conversations", include_in_schema=False)
async def list_conversation_metadata(
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """List conversation metadata for the JWT tenant, newest message first.

    Returns identifiers and state only — never message bodies, titles,
    context summaries, reasoning, or citations.  ``orderBy``/``order``
    select a whitelisted sort column and direction (illegal values fall
    back to the default ``lastMessageAt DESC``).  Optional identifiers and
    ``contextVersion`` are combined with AND; ``ragflowId`` matches either the
    RAGFlow chat or session identifier.
    """
    limit, offset = _pagination(request)
    clauses = ["tenant_id=?"]
    params: list[object] = [principal.tenant_id]
    status = (request.query_params.get("status") or "").strip()
    if status:
        clauses.append("status=?")
        params.append(status)
    for query_name, column in _CONVERSATION_FILTER_COLUMNS.items():
        value = (request.query_params.get(query_name) or "").strip()
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    ragflow_id = (request.query_params.get("ragflowId") or "").strip()
    if ragflow_id:
        clauses.append("(ragflow_chat_id=? OR ragflow_session_id=?)")
        params.extend([ragflow_id, ragflow_id])
    context_version = (request.query_params.get("contextVersion") or "").strip()
    if context_version:
        try:
            context_version_value = int(context_version)
        except ValueError:
            context_version_value = None
        if context_version_value is not None and context_version_value >= 0:
            clauses.append("context_version=?")
            params.append(context_version_value)
    order_column, order_direction = _resolve_ordering(
        request, _CONVERSATION_ORDER_COLUMNS, _DEFAULT_CONVERSATION_ORDER_BY
    )
    params.extend([limit + 1, offset])
    async with gateway.transaction(write=False) as conn:
        rows = await fetchall(
            conn,
            f"""SELECT {', '.join(_CONVERSATION_ITEM_KEYS)}
                FROM ext_v2_conversation
                WHERE {' AND '.join(clauses)}
                ORDER BY {order_column} {order_direction}, {_CONVERSATION_TIEBREAKER}
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
    has_more = len(rows) > limit
    return {
        "items": [
            {
                "conversationId": row["conversation_id"],
                "businessUserId": row["business_user_id"],
                "equipmentId": row["equipment_id"],
                "fixedAssetNo": row["fixed_asset_no"],
                "status": row["status"],
                "ragflowChatId": row["ragflow_chat_id"],
                "ragflowSessionId": row["ragflow_session_id"],
                "contextVersion": int(row["context_version"] or 0),
                "createdAt": row["created_at"],
                "lastMessageAt": row["last_message_at"],
            }
            for row in rows[:limit]
        ],
        "hasMore": has_more,
    }


_DOCUMENT_ITEM_FIELDS = (
    ("externalDocumentId", "external_document_id"),
    ("sourceVersionId", "source_version_id"),
    ("currentVersion", "current_version"),
    ("fileName", "file_name"),
    ("sourceKind", "source_kind"),
    ("parserProfile", "parser_profile"),
    ("parserProfileVersion", "parser_profile_version"),
    ("parserApplicationStatus", "parser_application_status"),
    ("sourceSystem", "source_system"),
    ("documentType", "document_type"),
    ("equipmentId", "equipment_id"),
    ("fixedAssetNo", "fixed_asset_no"),
    ("assetId", "asset_id"),
    ("syncStatus", "sync_status"),
    ("businessStatus", "business_status"),
    ("ragflowDatasetId", "ragflow_dataset_id"),
    ("ragflowDocumentId", "ragflow_document_id"),
    ("sourceSize", "source_size"),
    ("createdAt", "created_at"),
    ("updatedAt", "updated_at"),
    ("parsedAt", "parsed_at"),
    ("eamNotifiedAt", "eam_notified_at"),
)

_DOCUMENT_ORDER_COLUMNS = {
    "externalDocumentId": "d.external_document_id",
    "fileName": "d.file_name",
    "sourceSystem": "d.source_system",
    "documentType": "d.document_type",
    "equipmentId": "d.equipment_id",
    "fixedAssetNo": "d.fixed_asset_no",
    "assetId": "d.asset_id",
    "syncStatus": "d.sync_status",
    "businessStatus": "d.business_status",
    "sourceSize": "d.source_size",
    "createdAt": "d.created_at",
    "updatedAt": "d.updated_at",
    "parsedAt": "d.parsed_at",
    # Gateway 通知 EAM 的送达时间：取该文档所有已送达回调的最新时刻。
    "eamNotifiedAt": "eam_notified_at",
}
_DOCUMENT_FILTER_COLUMNS = {
    "externalDocumentId": "d.external_document_id",
    "sourceVersionId": "d.source_version_id",
    "fileName": "d.file_name",
    "equipmentId": "d.equipment_id",
    "fixedAssetNo": "d.fixed_asset_no",
    "assetId": "d.asset_id",
    "ragflowDocumentId": "d.ragflow_document_id",
}
_DEFAULT_DOCUMENT_ORDER_BY = "updatedAt"
_DOCUMENT_TIEBREAKER = "d.external_document_id ASC"

_DOCUMENT_SELECT_SQL = """SELECT d.id, d.tenant_id, d.source_system,
       d.external_document_id, d.source_version_id,
       d.current_version, d.file_name, d.source_kind, d.document_type,
       d.parser_profile, d.parser_profile_version,
       d.parser_application_status,
       d.media_type, d.source_page_count, d.department_id, d.security_level,
       d.document_subtype, d.source_document_type, d.ingest_state,
       d.source_state, d.source_state_reason, d.attempt_count,
       d.parse_retry_count, d.last_error_code, d.last_error_retryable,
       d.last_sync_at, d.source_updated_at,
       d.equipment_id, d.fixed_asset_no, d.asset_id, d.sync_status,
       d.business_status, d.ragflow_dataset_id, d.ragflow_document_id,
       d.source_size, d.created_at, d.updated_at, d.parsed_at,
       (SELECT MAX(cd.updated_at) FROM callback_delivery cd
         WHERE cd.tenant_id = d.tenant_id
           AND cd.source_system = d.source_system
           AND cd.external_document_id = d.external_document_id
           AND cd.state = 'delivered') AS eam_notified_at
FROM ext_document_map d"""


@router.get("/metadata/documents", include_in_schema=False)
async def list_document_metadata(
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """List document metadata for the JWT tenant, most recently updated first.

    Returns identifiers and state only — never file content, hashes, storage
    paths, ACL groups, error stacks, or credentials.  ``status`` filters the
    sync status, ``businessStatus`` the business status, and
    ``orderBy``/``order`` select a whitelisted sort column and direction
    (illegal values fall back to the default ``updatedAt DESC``).
    """
    limit, offset = _pagination(request)
    source_system = (
        request.query_params.get("sourceSystem")
        or request.query_params.get("source_system")
        or ""
    ).strip() or None
    status = (request.query_params.get("status") or "").strip() or None
    business_status = (
        request.query_params.get("businessStatus")
        or request.query_params.get("business_status")
        or ""
    ).strip() or None
    parser_application_status = (
        request.query_params.get("parserApplicationStatus")
        or request.query_params.get("parser_application_status")
        or ""
    ).strip() or None
    clauses = ["d.tenant_id=?"]
    params: list[object] = [principal.tenant_id]
    if source_system:
        clauses.append("d.source_system=?")
        params.append(source_system)
    if status:
        clauses.append("d.sync_status=?")
        params.append(status)
    if business_status:
        clauses.append("d.business_status=?")
        params.append(business_status)
    if parser_application_status:
        clauses.append("d.parser_application_status=?")
        params.append(parser_application_status)
    for query_name, column in _DOCUMENT_FILTER_COLUMNS.items():
        value = (request.query_params.get(query_name) or "").strip()
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    order_column, order_direction = _resolve_ordering(
        request, _DOCUMENT_ORDER_COLUMNS, _DEFAULT_DOCUMENT_ORDER_BY
    )
    params.extend([limit + 1, offset])
    async with gateway.transaction(write=False) as conn:
        rows = await fetchall(
            conn,
            f"""{_DOCUMENT_SELECT_SQL}
                WHERE {' AND '.join(clauses)}
                ORDER BY {order_column} {order_direction}, {_DOCUMENT_TIEBREAKER}
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
    has_more = len(rows) > limit
    return {
        "items": [
            {key: row[field] for key, field in _DOCUMENT_ITEM_FIELDS}
            for row in rows[:limit]
        ],
        "hasMore": has_more,
    }


def _safe_json_value(value: object, *, depth: int = 0) -> object:
    """Project persisted parser JSON without allowing credential-like keys."""
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, list):
        return [_safe_json_value(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, raw_value in list(value.items())[:128]:
            key = str(raw_key)
            normalized = key.lower().replace("_", "").replace("-", "")
            if any(term in normalized for term in (
                "password", "secret", "token", "apikey", "credential", "authorization", "cookie",
                "hash", "objectpath", "objectkey", "storagepath", "filepath", "bucket",
                "acl", "allowgroup", "denygroup", "prompt", "knowledge", "chainofthought",
            )):
                continue
            result[key] = _safe_json_value(raw_value, depth=depth + 1)
        return result
    return str(value)[:512]


def _parser_json(value: object) -> object:
    if not value:
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return {}
    return _safe_json_value(parsed)


def _document_item(row) -> dict[str, object]:
    return {key: row[field] for key, field in _DOCUMENT_ITEM_FIELDS}


def _document_metadata_detail(row) -> dict[str, object]:
    return {
        "mediaType": row["media_type"],
        "sourcePageCount": row["source_page_count"],
        "departmentId": row["department_id"],
        "securityLevel": row["security_level"],
        "documentSubtype": row["document_subtype"],
        "sourceDocumentType": row["source_document_type"],
        "ingestState": row["ingest_state"],
        "sourceState": row["source_state"],
        "sourceStateReason": row["source_state_reason"],
        "attemptCount": row["attempt_count"],
        "parseRetryCount": row["parse_retry_count"],
        "lastErrorCode": row["last_error_code"],
        "lastErrorRetryable": bool(row["last_error_retryable"]),
        "lastSyncAt": row["last_sync_at"],
        "sourceUpdatedAt": row["source_updated_at"],
    }


def _safe_ragflow_document(document: object) -> dict[str, object] | None:
    if not isinstance(document, dict):
        return None
    parser_config = document.get("parser_config")
    return {
        "run": str(document.get("run") or "") or None,
        "chunkMethod": str(document.get("chunk_method") or "") or None,
        "chunkCount": document.get("chunk_count"),
        "tokenCount": document.get("token_count"),
        "progress": document.get("progress"),
        "parserConfig": _safe_json_value(parser_config) if isinstance(parser_config, (dict, list)) else {},
    }


def _document_ragflow_client():
    from enterprise.gateway.app import _ragflow_client

    return _ragflow_client()


async def _find_document_row(gateway, principal: UserPrincipal, external_document_id: str, source_version_id: str | None):
    clauses = ["d.tenant_id=?", "d.external_document_id=?"]
    params: list[object] = [principal.tenant_id, external_document_id]
    if source_version_id:
        clauses.append("d.source_version_id=?")
        params.append(source_version_id)
    async with gateway.transaction(write=False) as conn:
        return await fetchone(
            conn,
            f"""{_DOCUMENT_SELECT_SQL}
                WHERE {' AND '.join(clauses)}
                ORDER BY d.current_version DESC, d.updated_at DESC
                LIMIT 1""",
            tuple(params),
        )


@router.get("/metadata/documents/{external_document_id}/chunks", include_in_schema=False)
async def list_document_chunks(
    external_document_id: str,
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Read one tenant document's parsed chunks through Gateway-owned RAGFlow access."""
    request_id = str(uuid.uuid4())
    external_document_id = (external_document_id or "").strip()
    source_version_id = (request.query_params.get("sourceVersionId") or "").strip() or None
    try:
        page = max(1, int(request.query_params.get("page", "1")))
        page_size = max(1, min(int(request.query_params.get("pageSize", "20")), 100))
    except ValueError:
        page, page_size = 1, 20
    row = await _find_document_row(gateway, principal, external_document_id, source_version_id)
    if row is None:
        return _error(404, "NOT_FOUND", request_id)
    dataset_id = str(row["ragflow_dataset_id"] or "").strip()
    document_id = str(row["ragflow_document_id"] or "").strip()
    if not dataset_id or not document_id:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size, "hasMore": False, "state": "not_ready"}
    try:
        client = _document_ragflow_client()
        result = await client.list_chunks(dataset_id, document_id, page=page, page_size=page_size, request_id=request_id)
    except RAGFlowAPIError:
        return _error(503, "RAGFLOW_UNAVAILABLE", request_id)
    except Exception:
        logger.warning("admin chunk read failed request_id=%s error_type=unexpected", request_id)
        return _error(503, "RAGFLOW_UNAVAILABLE", request_id)
    data = result.get("data") if isinstance(result, dict) else {}
    data = data if isinstance(data, dict) else {}
    raw_chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    items: list[dict[str, object]] = []
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue
        content = str(chunk.get("content") or "")
        items.append({
            "id": str(chunk.get("id") or ""),
            "documentId": str(chunk.get("document_id") or document_id),
            "content": content[:16000],
            "imageId": str(chunk.get("image_id") or "") or None,
            "docType": str(chunk.get("doc_type_kwd") or "") or None,
            "available": chunk.get("available"),
            "positions": _safe_json_value(chunk.get("positions")),
            "importantKeywords": _safe_json_value(chunk.get("important_keywords")),
        })
    try:
        total = int(data.get("total") or len(items))
    except (TypeError, ValueError):
        total = len(items)
    return {
        "items": items,
        "total": max(total, len(items)),
        "page": page,
        "pageSize": page_size,
        "hasMore": max(total, len(items)) > page * page_size,
        "state": "ready",
    }


@router.get("/metadata/documents/{external_document_id}", include_in_schema=False)
async def get_document_metadata_detail(
    external_document_id: str,
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Return safe enterprise metadata and best-effort RAGFlow parser readback."""
    request_id = str(uuid.uuid4())
    external_document_id = (external_document_id or "").strip()
    source_version_id = (request.query_params.get("sourceVersionId") or "").strip() or None
    row = await _find_document_row(gateway, principal, external_document_id, source_version_id)
    if row is None:
        return _error(404, "NOT_FOUND", request_id)
    ragflow: dict[str, object] | None = None
    ragflow_error: str | None = None
    dataset_id = str(row["ragflow_dataset_id"] or "").strip()
    document_id = str(row["ragflow_document_id"] or "").strip()
    if dataset_id and document_id:
        try:
            docs = await _document_ragflow_client().list_documents(
                dataset_id, document_id=document_id, page=1, page_size=1, request_id=request_id
            )
            if docs:
                ragflow = _safe_ragflow_document(docs[0])
            else:
                ragflow_error = "RAGFLOW_DOCUMENT_NOT_FOUND"
        except RAGFlowAPIError:
            ragflow_error = "RAGFLOW_UNAVAILABLE"
        except Exception:
            logger.warning("admin document read failed request_id=%s error_type=unexpected", request_id)
            ragflow_error = "RAGFLOW_UNAVAILABLE"
    parser = {
        "applicationStatus": row["parser_application_status"],
        "profile": row["parser_profile"],
        "profileVersion": row["parser_profile_version"],
        "expected": _parser_json(row["parser_expected_json"]),
        "configured": _parser_json(row["parser_configured_json"]),
        "executed": _parser_json(row["parser_executed_json"]),
        "ragflow": ragflow,
        "errorCode": ragflow_error,
    }
    return {
        "item": _document_item(row),
        "metadata": _document_metadata_detail(row),
        "parser": parser,
    }


@router.get("/metadata/summary", include_in_schema=False)
async def metadata_summary(
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Aggregate conversation/document counters for the JWT tenant.

    Grouped ``byXxx`` maps contain only statuses that actually have rows;
    totals are the sum of the grouped counts.  Counters only — no message
    bodies, titles, or document content.
    """
    async with gateway.transaction(write=False) as conn:
        conversation_rows = await fetchall(
            conn,
            "SELECT status, COUNT(*) AS n FROM ext_v2_conversation "
            "WHERE tenant_id=? GROUP BY status",
            (principal.tenant_id,),
        )
        sync_rows = await fetchall(
            conn,
            "SELECT sync_status, COUNT(*) AS n FROM ext_document_map "
            "WHERE tenant_id=? GROUP BY sync_status",
            (principal.tenant_id,),
        )
        business_rows = await fetchall(
            conn,
            "SELECT business_status, COUNT(*) AS n FROM ext_document_map "
            "WHERE tenant_id=? GROUP BY business_status",
            (principal.tenant_id,),
        )
    by_status = {row["status"]: int(row["n"]) for row in conversation_rows}
    by_sync_status = {row["sync_status"]: int(row["n"]) for row in sync_rows}
    by_business_status = {row["business_status"]: int(row["n"]) for row in business_rows}
    return {
        "conversations": {
            "total": sum(by_status.values()),
            "byStatus": by_status,
        },
        "documents": {
            "total": sum(by_sync_status.values()),
            "bySyncStatus": by_sync_status,
            "byBusinessStatus": by_business_status,
        },
    }


@router.get(
    "/metadata/conversations/{conversation_id}/messages",
    include_in_schema=False,
)
async def list_conversation_messages(
    conversation_id: str,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Return every message of one conversation for the session-management page.

    管理端"会话管理"页面需要回显对话正文：本端点按管理员要求返回
    user/assistant 消息内容和安全 citation 摘要（created_at 升序，硬上限
    500 条）。为避免把模型思考原文或临时票据带入管理界面，reasoning、
    attachments 和下载 URL 仍不返回。跨租户与不存在的会话一律 404，
    不区分暴露。
    """
    request_id = str(uuid.uuid4())
    conversation_id = (conversation_id or "").strip()
    if not conversation_id:
        return _error(422, "VALIDATION_ERROR", request_id)
    async with gateway.transaction(write=False) as conn:
        conversation = await fetchone(
            conn,
            "SELECT 1 FROM ext_v2_conversation "
            "WHERE conversation_id=? AND tenant_id=?",
            (conversation_id, principal.tenant_id),
        )
        if conversation is None:
            return _error(404, "NOT_FOUND", request_id)
        rows = await fetchall(
            conn,
            f"""SELECT message_id, role, content, status, citations_json, created_at
                FROM ext_v2_message
                WHERE conversation_id=? AND tenant_id=?
                ORDER BY created_at ASC, message_id ASC
                LIMIT {_MAX_MESSAGE_ITEMS}""",
            (conversation_id, principal.tenant_id),
        )
    return {
        "conversationId": conversation_id,
        "items": [
            {
                "messageId": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "status": row["status"],
                "citations": _admin_citations(row["citations_json"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ],
    }


def _diagnostics_from_row(row) -> dict | None:
    try:
        result = json.loads(row["result_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    diagnostics = result.get("_diagnostics") if isinstance(result, dict) else None
    return diagnostics if isinstance(diagnostics, dict) and diagnostics else None


def _diagnostics_outcome(diagnostics: dict) -> str | None:
    for event in reversed(diagnostics.get("events") or []):
        if not isinstance(event, dict) or event.get("type") != "outcome":
            continue
        data = event.get("data")
        if isinstance(data, dict) and data.get("outcome"):
            return str(data["outcome"])
    return None


@router.get("/diagnostics/traces", include_in_schema=False)
async def list_rag_diagnostics(
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """List private RAG traces for the JWT tenant without answer content."""
    limit, offset = _pagination(request)
    conversation_id = (request.query_params.get("conversationId") or "").strip()
    status = (request.query_params.get("status") or "").strip()
    clauses = ["tenant_id=?", "result_json LIKE ?"]
    params: list[object] = [principal.tenant_id, '%"_diagnostics"%']
    if conversation_id:
        clauses.append("conversation_id=?")
        params.append(conversation_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    params.extend([limit + 1, offset])
    async with gateway.transaction(write=False) as conn:
        rows = await fetchall(
            conn,
            f"""SELECT run_id, conversation_id, client_message_id, status,
                       result_json, created_at
                FROM ext_v2_message_run
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, run_id DESC
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
    items = []
    for row in rows[:limit]:
        diagnostics = _diagnostics_from_row(row)
        if diagnostics is None:
            continue
        items.append(
            {
                "runId": row["run_id"],
                "conversationId": row["conversation_id"],
                "clientMessageId": row["client_message_id"],
                "status": row["status"],
                "outcome": _diagnostics_outcome(diagnostics),
                "startedAt": diagnostics.get("startedAt"),
                "durationMs": diagnostics.get("durationMs"),
                "truncated": bool(diagnostics.get("truncated")),
                "createdAt": row["created_at"],
            }
        )
    return {"items": items, "hasMore": len(rows) > limit}


@router.get("/diagnostics/traces/{run_id}", include_in_schema=False)
async def get_rag_diagnostics(
    run_id: str,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Return one tenant-isolated trace; unknown and cross-tenant ids are 404."""
    request_id = str(uuid.uuid4())
    run_id = (run_id or "").strip()
    if not run_id:
        return _error(404, "NOT_FOUND", request_id)
    async with gateway.transaction(write=False) as conn:
        row = await fetchone(
            conn,
            """SELECT run_id, conversation_id, client_message_id, status,
                      result_json, created_at
               FROM ext_v2_message_run
               WHERE run_id=? AND tenant_id=?""",
            (run_id, principal.tenant_id),
        )
    diagnostics = _diagnostics_from_row(row) if row is not None else None
    if row is None or diagnostics is None:
        return _error(404, "NOT_FOUND", request_id)
    return {
        "runId": row["run_id"],
        "conversationId": row["conversation_id"],
        "clientMessageId": row["client_message_id"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "diagnostics": diagnostics,
    }
