"""WebUI login endpoints for the single local Gateway operator account."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from enterprise.gateway.auth.console_session import (
    CONSOLE_SESSION_COOKIE,
    CONSOLE_SESSION_COOKIE_PATH,
    ConsoleAuthSettings,
    FailedLoginLimiter,
    console_principal,
    console_session_from_request,
    create_console_session,
    request_origin_allowed,
    session_public_payload,
    verify_console_password,
)
from enterprise.gateway.auth.middleware import UserAuthError

router = APIRouter(
    prefix="/enterprise/api/v1/console/auth",
    tags=["console-auth"],
)

_failed_logins = FailedLoginLimiter()
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class ConsoleLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr = Field(min_length=1, max_length=128)
    password: StrictStr = Field(min_length=1, max_length=512)


def _client_key(request: Request, username: str) -> str:
    # Do not trust user-supplied forwarding headers for rate-limit identity.
    address = request.client.host if request.client else "unknown"
    return f"{address}:{username}"


def _invalid_login() -> None:
    raise UserAuthError(401, "CONSOLE_AUTH_INVALID", "账号或密码错误")


def _require_configured(settings: ConsoleAuthSettings) -> None:
    if not settings.configured:
        raise UserAuthError(
            503,
            "CONSOLE_AUTH_NOT_CONFIGURED",
            "本地运维登录尚未配置",
        )


def _set_session_cookie(
    response: Response, value: str, settings: ConsoleAuthSettings
) -> None:
    response.set_cookie(
        key=CONSOLE_SESSION_COOKIE,
        value=value,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=CONSOLE_SESSION_COOKIE_PATH,
    )


@router.post("/login", include_in_schema=False)
async def login(request: Request, body: ConsoleLoginRequest):
    settings = ConsoleAuthSettings.from_env()
    _require_configured(settings)
    if not request_origin_allowed(request, settings):
        raise UserAuthError(
            403,
            "CONSOLE_CSRF_REJECTED",
            "Request origin is not allowed",
        )

    key = _client_key(request, body.username)
    if _failed_logins.is_blocked(key):
        _invalid_login()
    valid = body.username == settings.username and verify_console_password(
        body.password, settings.password_hash
    )
    if not valid:
        _failed_logins.record_failure(key)
        _invalid_login()

    _failed_logins.clear(key)
    token, expires_at = create_console_session(settings, now=int(time.time()))
    session = {
        "username": settings.username,
        "tenantId": settings.tenant_id,
        "issuedAt": expires_at - settings.session_ttl_seconds,
        "expiresAt": expires_at,
    }
    response = JSONResponse(
        content=session_public_payload(session),
        headers=_NO_STORE_HEADERS,
    )
    _set_session_cookie(response, token, settings)
    return response


@router.get("/me", include_in_schema=False)
async def me(request: Request):
    settings = ConsoleAuthSettings.from_env()
    session = console_session_from_request(request, settings)
    if session is None:
        code = (
            "AUTH_TOKEN_INVALID"
            if request.cookies.get(CONSOLE_SESSION_COOKIE)
            else "AUTH_TOKEN_MISSING"
        )
        raise UserAuthError(401, code, "Console session is invalid or expired")
    return JSONResponse(
        content={
            **session_public_payload(session),
            **console_principal(session).to_safe_dict(),
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    settings = ConsoleAuthSettings.from_env()
    if not request_origin_allowed(request, settings):
        raise UserAuthError(
            403,
            "CONSOLE_CSRF_REJECTED",
            "Request origin is not allowed",
        )
    response = Response(status_code=204, headers=_NO_STORE_HEADERS)
    response.delete_cookie(
        key=CONSOLE_SESSION_COOKIE,
        path=CONSOLE_SESSION_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


__all__ = ["ConsoleLoginRequest", "router"]
