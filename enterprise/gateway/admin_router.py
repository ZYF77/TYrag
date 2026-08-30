"""Internal system-admin management APIs for the gateway settings page.

These endpoints are NOT part of the external OpenAPI contract: every route
sets ``include_in_schema=False`` and requires the ``admin`` capability
(mapped from the ``system_admin`` role).  They are strictly read-only except
for the EAM reachability probe, which performs a server-side GET against the
configured origin's JWKS path and never writes to the database.

Responses never contain HMAC secrets, API keys, credential material, file
hashes, or storage paths.  The "no message bodies" convention applies to the
metadata LIST endpoints only: the conversation-session endpoint
(``GET /metadata/conversations/{id}/messages``) intentionally echoes the
user/assistant message text for the admin session-management page, while it
still never returns citations, reasoning, attachments, or credentials.
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
from pydantic import BaseModel, Field

from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.callback_delivery import CallbackEndpoint, parse_callback_endpoints
from enterprise.gateway.config import config
from enterprise.gateway.db.dialect import fetchall, fetchone
from enterprise.gateway.sync.models import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/admin/system", tags=["system-admin"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200
_MAX_MESSAGE_ITEMS = 500
_PROBE_PATH = "/.well-known/jwks.json"
_PROBE_TIMEOUT_SECONDS = 5.0


async def get_db():
    """Reuse the app gateway dependency while keeping this router testable."""
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    value = dependency()
    return await value if inspect.iscoroutine(value) else value


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
        "enabled": bool(config.callback_enabled),
        "credentialConfigured": bool(endpoint.secret or endpoint.key_id),
    }


@router.get("/integrations", include_in_schema=False)
async def list_integrations(
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    """Show the RAGFlow connection card and tenant-visible callback bindings."""
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
        },
        "callbacksEnabled": bool(config.callback_enabled),
        "callbacks": [
            _callback_item(binding, callbacks[binding]) for binding in sorted(callbacks)
        ],
    }


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
    back to the default ``lastMessageAt DESC``).
    """
    limit, offset = _pagination(request)
    clauses = ["tenant_id=?"]
    params: list[object] = [principal.tenant_id]
    status = (request.query_params.get("status") or "").strip()
    if status:
        clauses.append("status=?")
        params.append(status)
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
_DEFAULT_DOCUMENT_ORDER_BY = "updatedAt"
_DOCUMENT_TIEBREAKER = "d.external_document_id ASC"

_DOCUMENT_SELECT_SQL = """SELECT d.id, d.tenant_id, d.source_system,
       d.external_document_id, d.source_version_id,
       d.current_version, d.file_name, d.source_kind, d.document_type,
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
    user/assistant 消息内容（created_at 升序，硬上限 500 条），但仍绝不
    返回 citations、reasoning、附件或任何凭据材料。跨租户与不存在的
    会话一律 404，不区分暴露。
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
            f"""SELECT message_id, role, content, status, created_at
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
                "createdAt": row["created_at"],
            }
            for row in rows
        ],
    }
