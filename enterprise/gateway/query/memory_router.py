"""P2 skeleton: end-user Memory self-service APIs.

GET/PATCH/DELETE /enterprise/api/v1/ai/memory/me[/{id}]
subject is derived only from Token (UserPrincipal) — never from query/body userId.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query.memory_subject import memory_subject_from_principal
from enterprise.gateway.query.user_memory import (
    enterprise_memory_id,
    get_memory_client,
    memory_config_ready,
    user_memory_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/ai/memory", tags=["user-memory"])


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "data": None},
    )


def _reject_client_user_id(request: Request, body: dict | None = None) -> JSONResponse | None:
    """Reject any client-reported userId / businessUserId / subject override."""
    banned_keys = {
        "userid",
        "user_id",
        "businessuserid",
        "business_user_id",
        "subject",
        "eamsubject",
    }
    for key in request.query_params.keys():
        if key.lower().replace("-", "") in banned_keys or key.lower() in {
            "userId",
            "user_id",
            "businessUserId",
            "subject",
        }:
            return _error(
                400,
                "USER_ID_NOT_ALLOWED",
                "userId/subject must not be supplied by the client",
            )
    if isinstance(body, dict):
        for key in body.keys():
            if str(key).lower().replace("-", "_") in {
                "userid",
                "user_id",
                "businessuserid",
                "business_user_id",
                "subject",
            }:
                return _error(
                    400,
                    "USER_ID_NOT_ALLOWED",
                    "userId/subject must not be supplied by the client",
                )
    return None


def _subject_or_error(principal: UserPrincipal) -> tuple[str | None, JSONResponse | None]:
    try:
        return memory_subject_from_principal(principal), None
    except ValueError as exc:
        return None, _error(400, "INVALID_PRINCIPAL", str(exc))


class MemoryPatchRequest(BaseModel):
    """Optional status toggle; identity fields are rejected elsewhere."""

    status: bool | None = Field(default=None)


@router.get("/me")
async def list_my_memory(
    request: Request,
    limit: int = 20,
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    rejected = _reject_client_user_id(request)
    if rejected is not None:
        return rejected
    if not user_memory_enabled():
        return {"code": 0, "message": "user memory disabled", "data": {"items": []}}
    if not memory_config_ready():
        return _error(
            503,
            "MEMORY_NOT_CONFIGURED",
            "ENTERPRISE_MEMORY_ID is required when user memory is enabled",
        )
    subject, err = _subject_or_error(principal)
    if err is not None:
        return err
    try:
        client = get_memory_client()
        items = await client.list_messages(
            memory_id=enterprise_memory_id(),
            user_id=subject,
            limit=max(1, min(int(limit), 100)),
        )
    except Exception as exc:
        logger.warning("list_my_memory_failed error_type=%s", type(exc).__name__)
        return _error(502, "MEMORY_UPSTREAM_ERROR", "Failed to list memory messages")
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "subject": subject,
            "items": items,
        },
    }


@router.delete("/me/{message_id}")
async def forget_my_memory(
    message_id: int,
    request: Request,
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    rejected = _reject_client_user_id(request)
    if rejected is not None:
        return rejected
    if not memory_config_ready():
        return _error(
            503,
            "MEMORY_NOT_CONFIGURED",
            "ENTERPRISE_MEMORY_ID is required when user memory is enabled",
        )
    subject, err = _subject_or_error(principal)
    if err is not None:
        return err
    # Ownership check: list/filter then forget only if subject owns the message.
    try:
        client = get_memory_client()
        owned = await client.list_messages(
            memory_id=enterprise_memory_id(),
            user_id=subject,
            limit=100,
        )
        if not any(int(item.get("message_id") or 0) == int(message_id) for item in owned):
            return _error(404, "NOT_FOUND", "Memory message not found for current user")
        await client.forget_message(
            memory_id=enterprise_memory_id(),
            message_id=message_id,
        )
    except Exception as exc:
        logger.warning("forget_my_memory_failed error_type=%s", type(exc).__name__)
        return _error(502, "MEMORY_UPSTREAM_ERROR", "Failed to forget memory message")
    return {"code": 0, "message": "ok", "data": {"forgotten": True, "messageId": message_id}}


@router.patch("/me/{message_id}")
async def patch_my_memory(
    message_id: int,
    request: Request,
    body: MemoryPatchRequest | None = None,
    principal: UserPrincipal = Depends(require_capability("ask")),
):
    """Skeleton: reject client userId; status updates not wired to RF PUT yet."""
    payload: dict[str, Any] = {}
    if body is not None:
        payload = body.model_dump(exclude_none=True)
    # Also inspect raw JSON for banned identity fields.
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            rejected = _reject_client_user_id(request, raw)
            if rejected is not None:
                return rejected
    except Exception:
        rejected = _reject_client_user_id(request, payload)
        if rejected is not None:
            return rejected
    rejected = _reject_client_user_id(request, payload)
    if rejected is not None:
        return rejected
    _subject, err = _subject_or_error(principal)
    if err is not None:
        return err
    if not memory_config_ready():
        return _error(
            503,
            "MEMORY_NOT_CONFIGURED",
            "ENTERPRISE_MEMORY_ID is required when user memory is enabled",
        )
    return _error(
        501,
        "NOT_IMPLEMENTED",
        "PATCH memory status is reserved; use DELETE /me/{id} to forget",
    )