"""Current EAM equipment identity projection and sync endpoints.

The stable equipment id is the key.  Fixed-asset and asset identifiers are
current projections that can change through the EAM service contract.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.sync.models import (
    OutboxEvent,
    enqueue_outbox,
    get_outbox_by_event_id,
    reset_outbox_to_pending,
    utc_now,
)

IDENTITY_EVENT_TYPE = "equipment_identity_update"
IDENTITY_EVENT_PREFIX = "equipment-identity:"
MAX_IDENTITY_VERSION = 2_147_483_647


class IdentityConflictError(ValueError):
    """The EAM snapshot conflicts with the current identity version."""


@dataclass(frozen=True)
class CurrentEquipmentIdentity:
    tenant_id: str
    equipment_id: str
    fixed_asset_no: str | None
    asset_id: str | None
    source_system: str
    identity_version: int
    updated_at: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "equipmentId": self.equipment_id,
            "fixedAssetNo": self.fixed_asset_no,
            "assetId": self.asset_id,
            "sourceSystem": self.source_system,
            "identityVersion": self.identity_version,
            "updatedAt": self.updated_at,
        }


def normalize_identity_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def identity_event_id(tenant_id: str, equipment_id: str, version: int) -> str:
    return f"{IDENTITY_EVENT_PREFIX}{tenant_id}:{equipment_id}:{version}"


def identity_event_payload(identity: CurrentEquipmentIdentity) -> str:
    return json.dumps(
        {
            "tenantId": identity.tenant_id,
            "equipmentId": identity.equipment_id,
            "fixedAssetNo": identity.fixed_asset_no,
            "assetId": identity.asset_id,
            "sourceSystem": identity.source_system,
            "identityVersion": identity.identity_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _identity_from_row(row: Any) -> CurrentEquipmentIdentity:
    return CurrentEquipmentIdentity(
        tenant_id=str(row["tenant_id"]),
        equipment_id=str(row["equipment_id"]),
        fixed_asset_no=normalize_identity_value(row["fixed_asset_no"]),
        asset_id=normalize_identity_value(row["asset_id"]),
        source_system=str(row["source_system"] or ""),
        identity_version=int(row["identity_version"] or 0),
        updated_at=str(row["updated_at"] or ""),
    )


async def get_current_identity(
    conn, tenant_id: str, equipment_id: str
) -> CurrentEquipmentIdentity | None:
    row = await fetchone(
        conn,
        """SELECT tenant_id, equipment_id, fixed_asset_no, asset_id,
                  source_system, identity_version, updated_at
             FROM ext_asset_registry
            WHERE tenant_id=? AND equipment_id=?""",
        (tenant_id, equipment_id),
    )
    return _identity_from_row(row) if row else None


async def resolve_current_identity(
    conn, tenant_id: str, identifier: str
) -> CurrentEquipmentIdentity | None:
    row = await fetchone(
        conn,
        """SELECT tenant_id, equipment_id, fixed_asset_no, asset_id,
                  source_system, identity_version, updated_at
             FROM ext_asset_registry
            WHERE tenant_id=?
              AND (equipment_id=? OR fixed_asset_no=? OR asset_id=?)
            ORDER BY equipment_id
            LIMIT 1""",
        (tenant_id, identifier, identifier, identifier),
    )
    return _identity_from_row(row) if row else None


async def list_current_identities(
    conn,
    tenant_id: str,
    *,
    identifier: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[CurrentEquipmentIdentity], bool]:
    clauses = ["r.tenant_id=?"]
    params: list[object] = [tenant_id]
    if identifier:
        clauses.append(
            "(r.equipment_id=? OR r.fixed_asset_no=? OR r.asset_id=?)"
        )
        params.extend([identifier, identifier, identifier])
    params.extend([limit + 1, offset])
    rows = await fetchall(
        conn,
        f"""SELECT r.tenant_id, r.equipment_id, r.fixed_asset_no, r.asset_id,
                    r.source_system, r.identity_version, r.updated_at
               FROM ext_asset_registry r
              WHERE {' AND '.join(clauses)}
              ORDER BY r.equipment_id
              LIMIT ? OFFSET ?""",
        params,
    )
    items = [_identity_from_row(row) for row in rows[:limit]]
    return items, len(rows) > limit


async def equipment_document_counts(
    conn, tenant_id: str, equipment_ids: list[str]
) -> dict[str, int]:
    if not equipment_ids:
        return {}
    placeholders = ", ".join("?" for _ in equipment_ids)
    rows = await fetchall(
        conn,
        f"""SELECT equipment_id, COUNT(*) AS document_count
               FROM ext_document_map
              WHERE tenant_id=? AND equipment_id IN ({placeholders})
              GROUP BY equipment_id""",
        [tenant_id, *equipment_ids],
    )
    return {str(row["equipment_id"]): int(row["document_count"] or 0) for row in rows}


async def latest_identity_outbox(conn, tenant_id: str, equipment_id: str) -> dict[str, Any]:
    row = await fetchone(
        conn,
        """SELECT status, last_error_code, last_error_message, updated_at
             FROM sync_outbox
            WHERE event_type=? AND tenant_id=? AND external_document_id=?
            ORDER BY id DESC
            LIMIT 1""",
        (IDENTITY_EVENT_TYPE, tenant_id, equipment_id),
    )
    if not row:
        return {"status": "not_queued"}
    return {
        "status": str(row["status"] or "pending"),
        "errorCode": row["last_error_code"],
        "errorMessage": row["last_error_message"],
        "updatedAt": row["updated_at"],
    }


async def upsert_current_identity(
    conn,
    *,
    tenant_id: str,
    source_system: str,
    equipment_id: str,
    fixed_asset_no: str | None,
    asset_id: str | None,
    identity_version: int,
) -> tuple[CurrentEquipmentIdentity, str, bool]:
    equipment_id = equipment_id.strip()
    source_system = source_system.strip()
    fixed_asset_no = normalize_identity_value(fixed_asset_no)
    asset_id = normalize_identity_value(asset_id)
    if not equipment_id or not source_system:
        raise ValueError("tenant, source system and equipment id are required")
    if not 1 <= identity_version <= MAX_IDENTITY_VERSION:
        raise ValueError("identity version is outside the supported range")

    existing = await get_current_identity(conn, tenant_id, equipment_id)
    if existing:
        incoming = (
            fixed_asset_no,
            asset_id,
            source_system,
        )
        current = (
            existing.fixed_asset_no,
            existing.asset_id,
            existing.source_system,
        )
        if identity_version < existing.identity_version:
            return existing, "stale", False
        if identity_version == existing.identity_version:
            if incoming != current:
                raise IdentityConflictError(
                    "identity version already contains different values"
                )
            return existing, "idempotent", False

    now = utc_now()
    if existing:
        await exec_sql(
            conn,
            """UPDATE ext_asset_registry
                  SET fixed_asset_no=?, asset_id=?, source_system=?,
                      identity_version=?, updated_at=?
                WHERE tenant_id=? AND equipment_id=?""",
            (
                fixed_asset_no,
                asset_id,
                source_system,
                identity_version,
                now,
                tenant_id,
                equipment_id,
            ),
        )
    else:
        await exec_sql(
            conn,
            """INSERT INTO ext_asset_registry
               (tenant_id, equipment_id, fixed_asset_no, asset_id,
                source_system, identity_version, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                tenant_id,
                equipment_id,
                fixed_asset_no,
                asset_id,
                source_system,
                identity_version,
                now,
            ),
        )

    # Gateway's document map is the query-side identity projection.  Keep it
    # current so stale document retries cannot restore an old alias.
    await exec_sql(
        conn,
        """UPDATE ext_document_map
              SET fixed_asset_no=?, asset_id=?, updated_at=?
            WHERE tenant_id=? AND equipment_id=?""",
        (fixed_asset_no, asset_id, now, tenant_id, equipment_id),
    )
    current = await get_current_identity(conn, tenant_id, equipment_id)
    if current is None:
        raise RuntimeError("identity update was not persisted")
    return current, "updated", True


async def retry_identity_sync(conn, tenant_id: str, equipment_id: str) -> dict[str, Any]:
    """Re-enter the latest identity outbox row, or enqueue from current mapping.

    Idempotent for pending/processing rows. Never triggers upload/parse.
    """
    identity = await get_current_identity(conn, tenant_id, equipment_id)
    if identity is None:
        raise LookupError("equipment identity not found")
    latest = await latest_identity_outbox(conn, tenant_id, equipment_id)
    status = str(latest.get("status") or "not_queued")
    if status in {"pending", "processing"}:
        return {**identity.to_public_dict(), "sync": latest, "retried": False}

    event_id = identity_event_id(tenant_id, equipment_id, identity.identity_version)
    existing = await get_outbox_by_event_id(conn, event_id)
    if existing is not None:
        reset = await reset_outbox_to_pending(conn, event_id)
        sync = {
            "status": reset.status if reset else "pending",
            "errorCode": None,
            "errorMessage": None,
            "updatedAt": reset.updated_at if reset else None,
        }
        return {**identity.to_public_dict(), "sync": sync, "retried": True}

    event = OutboxEvent(
        event_id=event_id,
        event_type=IDENTITY_EVENT_TYPE,
        tenant_id=identity.tenant_id,
        source_system=identity.source_system,
        external_document_id=identity.equipment_id,
        source_version_id=str(identity.identity_version),
        payload=identity_event_payload(identity),
    )
    queued = await enqueue_outbox(conn, event)
    sync = {
        "status": queued.status or "pending",
        "errorCode": queued.last_error_code,
        "errorMessage": queued.last_error_message,
        "updatedAt": queued.updated_at,
    }
    return {**identity.to_public_dict(), "sync": sync, "retried": True}


DEFAULT_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._\\-]{3,127}"
MAX_IDENTIFIER_PATTERN_LENGTH = 512


def validate_identifier_pattern(pattern: str) -> str:
    pattern = pattern.strip()
    if not pattern or len(pattern) > MAX_IDENTIFIER_PATTERN_LENGTH:
        raise ValueError("identifier pattern must contain 1-512 characters")
    if any(token in pattern for token in ("(?", "\\\\1", "\\\\g<")):
        raise ValueError("identifier pattern contains unsupported constructs")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError("identifier pattern is invalid") from exc
    if compiled.search(""):
        raise ValueError("identifier pattern must not match an empty string")
    return pattern


async def get_recognition_settings(conn, tenant_id: str) -> dict[str, Any]:
    row = await fetchone(
        conn,
        """SELECT pattern, enabled, config_version, updated_at, updated_by
             FROM gateway_equipment_recognition_settings
            WHERE tenant_id=?""",
        (tenant_id,),
    )
    if row is None:
        return {
            "pattern": DEFAULT_IDENTIFIER_PATTERN,
            "enabled": True,
            "configVersion": 0,
            "updatedAt": None,
            "updatedBy": None,
        }
    return {
        "pattern": str(row["pattern"]),
        "enabled": bool(row["enabled"]),
        "configVersion": int(row["config_version"] or 0),
        "updatedAt": row["updated_at"],
        "updatedBy": row["updated_by"],
    }


async def save_recognition_settings(
    conn,
    *,
    tenant_id: str,
    pattern: str,
    enabled: bool,
    expected_version: int,
    updated_by: str,
) -> dict[str, Any]:
    pattern = validate_identifier_pattern(pattern)
    current = await get_recognition_settings(conn, tenant_id)
    if expected_version != current["configVersion"]:
        raise IdentityConflictError("recognition configuration version conflict")
    version = expected_version + 1
    now = utc_now()
    await exec_sql(
        conn,
        """INSERT INTO gateway_equipment_recognition_settings
           (tenant_id, pattern, enabled, config_version, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(tenant_id) DO UPDATE SET
             pattern=excluded.pattern,
             enabled=excluded.enabled,
             config_version=excluded.config_version,
             updated_at=excluded.updated_at,
             updated_by=excluded.updated_by""",
        (tenant_id, pattern, 1 if enabled else 0, version, now, updated_by),
    )
    return await get_recognition_settings(conn, tenant_id)


def preview_identifier_pattern(text: str, pattern: str) -> list[str]:
    if len(text) > 4096:
        raise ValueError("preview text is too long")
    compiled = re.compile(validate_identifier_pattern(pattern))
    values: list[str] = []
    for match in compiled.finditer(text):
        value = match.group(0)
        if value and value not in values:
            values.append(value)
        if len(values) >= 64:
            break
    return values


def _service_scope_allowed(
    principal: ServicePrincipal, tenant_id: str, source_system: str
) -> bool:
    if principal.allowed_bindings:
        return (tenant_id, source_system) in principal.allowed_bindings
    return principal.source_system in {"service", "anonymous", source_system}


async def get_db():
    from enterprise.gateway import app as app_module

    dependency = app_module.app.dependency_overrides.get(
        app_module.get_gateway_db, app_module.get_gateway_db
    )
    value = dependency()
    return await value if hasattr(value, "__await__") else value


class IdentityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenantId: str = Field(min_length=1, max_length=64)
    sourceSystem: str = Field(min_length=1, max_length=64)
    fixedAssetNo: str | None = Field(default=None, max_length=128)
    assetId: str | None = Field(default=None, max_length=128)
    identityVersion: StrictInt = Field(gt=0, le=MAX_IDENTITY_VERSION)


external_router = APIRouter(
    prefix="/enterprise/api/v3/equipment-identities",
    tags=["equipment-identities"],
)


@external_router.put("/{equipment_id}")
async def update_equipment_identity(
    equipment_id: str,
    payload: IdentityUpdateRequest,
    gateway=Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    request_id = identity_event_id(payload.tenantId, equipment_id, payload.identityVersion)
    if not _service_scope_allowed(principal, payload.tenantId, payload.sourceSystem):
        return JSONResponse(
            status_code=403,
            content={"code": "ACL_DENIED", "message": "Access denied", "requestId": request_id},
        )
    try:
        async with gateway.transaction(write=True) as conn:
            identity, outcome, changed = await upsert_current_identity(
                conn,
                tenant_id=payload.tenantId,
                source_system=payload.sourceSystem,
                equipment_id=equipment_id,
                fixed_asset_no=payload.fixedAssetNo,
                asset_id=payload.assetId,
                identity_version=payload.identityVersion,
            )
            if changed:
                event = OutboxEvent(
                    event_id=request_id,
                    event_type=IDENTITY_EVENT_TYPE,
                    tenant_id=identity.tenant_id,
                    source_system=identity.source_system,
                    external_document_id=identity.equipment_id,
                    source_version_id=str(identity.identity_version),
                    payload=identity_event_payload(identity),
                )
                await enqueue_outbox(conn, event)
    except IdentityConflictError:
        return JSONResponse(
            status_code=409,
            content={"code": "IDENTITY_VERSION_CONFLICT", "message": "Identity version conflict", "requestId": request_id},
        )
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "Invalid equipment identity", "requestId": request_id},
        )
    return JSONResponse(
        status_code=202 if changed else 200,
        content={
            **identity.to_public_dict(),
            "outcome": outcome,
            "syncStatus": "pending" if changed else "unchanged",
            "requestId": request_id,
        },
    )


@external_router.get("/{equipment_id}")
async def get_equipment_identity(
    equipment_id: str,
    tenantId: str,
    sourceSystem: str,
    gateway=Depends(get_db),
    principal: ServicePrincipal = Depends(require_service_principal),
):
    if not _service_scope_allowed(principal, tenantId, sourceSystem):
        return JSONResponse(status_code=403, content={"code": "ACL_DENIED", "message": "Access denied"})
    async with gateway.transaction(write=False) as conn:
        identity = await get_current_identity(conn, tenantId, equipment_id)
        if identity is None:
            return JSONResponse(status_code=404, content={"code": "IDENTITY_NOT_FOUND", "message": "Equipment identity not found"})
        sync = await latest_identity_outbox(conn, tenantId, equipment_id)
    return {**identity.to_public_dict(), "sync": sync}



class RecognitionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=MAX_IDENTIFIER_PATTERN_LENGTH)
    enabled: StrictBool = True
    configVersion: StrictInt = Field(ge=0)


class RecognitionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=4096)
    pattern: str | None = Field(default=None, max_length=MAX_IDENTIFIER_PATTERN_LENGTH)


admin_router = APIRouter(prefix="/enterprise/api/v1/admin/system", tags=["system-admin"])


@admin_router.get("/equipment-identities", include_in_schema=False)
async def list_equipment_identities(
    request: Request,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        limit, offset = 20, 0
    identifier = (request.query_params.get("identifier") or "").strip() or None
    async with gateway.transaction(write=False) as conn:
        items, has_more = await list_current_identities(
            conn, principal.tenant_id, identifier=identifier, limit=limit, offset=offset
        )
        counts = await equipment_document_counts(
            conn, principal.tenant_id, [item.equipment_id for item in items]
        )
        result = []
        for item in items:
            result.append({
                **item.to_public_dict(),
                "documentCount": counts.get(item.equipment_id, 0),
                "sync": await latest_identity_outbox(conn, principal.tenant_id, item.equipment_id),
            })
    return {"items": result, "hasMore": has_more}

@admin_router.get("/equipment-recognition", include_in_schema=False)
async def get_equipment_recognition(
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    async with gateway.transaction(write=False) as conn:
        return await get_recognition_settings(conn, principal.tenant_id)


@admin_router.put("/equipment-recognition", include_in_schema=False)
async def update_equipment_recognition(
    payload: RecognitionSettingsRequest,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    try:
        async with gateway.transaction(write=True) as conn:
            return await save_recognition_settings(
                conn,
                tenant_id=principal.tenant_id,
                pattern=payload.pattern,
                enabled=payload.enabled,
                expected_version=payload.configVersion,
                updated_by=principal.subject,
            )
    except IdentityConflictError:
        return JSONResponse(
            status_code=409,
            content={"code": "CONFIG_VERSION_CONFLICT", "message": "Recognition configuration changed", "requestId": identity_event_id(principal.tenant_id, "recognition", payload.configVersion)},
        )
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "Invalid recognition configuration"},
        )


@admin_router.post("/equipment-recognition/preview", include_in_schema=False)
async def preview_equipment_recognition(
    payload: RecognitionPreviewRequest,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    async with gateway.transaction(write=False) as conn:
        settings = await get_recognition_settings(conn, principal.tenant_id)
        pattern = payload.pattern or settings["pattern"]
        try:
            candidates = preview_identifier_pattern(payload.text, pattern)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"code": "VALIDATION_ERROR", "message": "Invalid recognition pattern"},
            )
        matches = []
        for candidate in candidates:
            identity = await resolve_current_identity(conn, principal.tenant_id, candidate)
            matches.append({
                "candidate": candidate,
                "equipmentId": identity.equipment_id if identity else None,
                "matched": identity is not None,
            })
    return {"items": matches, "pattern": pattern, "enabled": settings["enabled"]}




@admin_router.get("/equipment-identities/{equipment_id}/sync", include_in_schema=False)
async def equipment_identity_sync_status(
    equipment_id: str,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    async with gateway.transaction(write=False) as conn:
        identity = await get_current_identity(conn, principal.tenant_id, equipment_id)
        if identity is None:
            return JSONResponse(status_code=404, content={"code": "IDENTITY_NOT_FOUND", "message": "Equipment identity not found"})
        return {**identity.to_public_dict(), "sync": await latest_identity_outbox(conn, principal.tenant_id, equipment_id)}

@admin_router.post("/equipment-identities/{equipment_id}/sync", include_in_schema=False)
async def retry_equipment_identity_sync(
    equipment_id: str,
    gateway=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("admin")),
):
    try:
        async with gateway.transaction(write=True) as conn:
            return await retry_identity_sync(conn, principal.tenant_id, equipment_id)
    except LookupError:
        return JSONResponse(
            status_code=404,
            content={"code": "IDENTITY_NOT_FOUND", "message": "Equipment identity not found"},
        )

