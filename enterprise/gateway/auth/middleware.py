"""FastAPI dependencies for end-user, WebUI, and service authentication.

External EAM user JWTs and service/HMAC credentials remain strictly separate;
the console helper only adds the local WebUI cookie to user-facing routes.
"""
from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from enterprise.gateway.auth.console_session import (
    CONSOLE_SESSION_COOKIE,
    ConsoleAuthSettings,
    console_principal,
    console_session_from_request,
    request_origin_allowed,
)
from enterprise.gateway.auth.token_validator import JWTValidator, TokenValidationError
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.models.ext_user_map import ExtUserMap, ExtUserMapRepo

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

_AUTH_ERROR_MESSAGES = {
    "AUTH_TOKEN_MISSING": "Authentication token is required",
    "AUTH_TOKEN_EXPIRED": "Token has expired",
    "CONFIG_ERROR": "Authentication is not configured",
}


def _remember_audit_username(request: Request, principal: UserPrincipal) -> None:
    """Attach only a display-safe identity to the request audit context.

    The HTTP audit ring needs to tell operators which authenticated user made a
    request, but it must never retain bearer tokens or other credential data.
    """
    username = (principal.business_user_id or principal.display_name or principal.subject).strip()
    if username:
        request.state.audit_username = username


class UserAuthError(Exception):
    """Authentication error returned to clients in the ErrorResponse shape."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def require_user_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> UserPrincipal:
    """FastAPI dependency: validate end-user JWT and return UserPrincipal.

    Uses configurable claim mapping - never trusts request body for identity fields.
    Missing ext_user_map rows are JIT-provisioned as active on first valid JWT.
    If ext_user_map has status=disabled, returns 403.
    """
    if credentials is None or not credentials.credentials:
        raise UserAuthError(
            401,
            "AUTH_TOKEN_MISSING",
            "Authentication token is required",
        )

    token = credentials.credentials
    validator = _get_validator()

    try:
        claims = validator.validate(token)
    except TokenValidationError as e:
        logger.warning("user JWT rejected code=%s reason=%s", e.code, e.message)
        raise UserAuthError(
            401,
            e.code,
            _AUTH_ERROR_MESSAGES.get(e.code, e.message or "Authentication token is invalid"),
        ) from e

    principal = UserPrincipal.from_validated_claims(
        claims,
        validator._config.claim_map,
    )

    repo = _get_repo()
    try:
        await repo.ensure_table()
        mapping = await repo.get_mapping(principal.tenant_id, principal.subject)
        if mapping is None:
            await repo.insert_mapping(
                ExtUserMap(
                    tenant_id=principal.tenant_id,
                    business_subject=principal.subject,
                    business_user_id=principal.business_user_id,
                    status="active",
                    mapping_strategy="B",
                )
            )
            mapping = await repo.get_mapping(principal.tenant_id, principal.subject)
        if mapping is None:
            raise UserAuthError(
                403,
                "AUTH_USER_MAPPING_MISSING",
                "User mapping not found",
            )
        if mapping.get("status") == "disabled":
            raise UserAuthError(
                403,
                "AUTH_USER_DISABLED",
                "User account is disabled",
            )

        principal = UserPrincipal(
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            subject=principal.subject,
            display_name=principal.display_name,
            department_ids=principal.department_ids,
            role_codes=principal.role_codes,
            group_ids=principal.group_ids,
            security_level=principal.security_level,
            token_issued_at=principal.token_issued_at,
            token_expires_at=principal.token_expires_at,
            mapping_status=mapping.get("status", "active"),
            capabilities=principal.capabilities,
        )
    finally:
        await repo.close()

    _remember_audit_username(request, principal)
    return principal


async def require_console_or_user_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> UserPrincipal:
    """Authenticate an EAM user or the local WebUI operator session.

    Service/HMAC dependencies never use this helper.  A supplied Bearer token
    remains authoritative; the local cookie is only a fallback for browser
    user routes and is protected by same-origin checks on state-changing
    requests.
    """

    # Keep existing test dependency overrides working while the production
    # path remains explicitly Bearer-or-cookie based.
    override = request.app.dependency_overrides.get(require_user_principal)
    if credentials is not None and credentials.credentials:
        if override is not None:
            value = override()
            principal = await value if inspect.isawaitable(value) else value
            _remember_audit_username(request, principal)
            return principal
        return await require_user_principal(request, credentials)

    if override is not None and not request.cookies.get(CONSOLE_SESSION_COOKIE):
        value = override()
        principal = await value if inspect.isawaitable(value) else value
        _remember_audit_username(request, principal)
        return principal

    settings = ConsoleAuthSettings.from_env()
    session = console_session_from_request(request, settings)
    if session is None:
        if request.cookies.get(CONSOLE_SESSION_COOKIE):
            raise UserAuthError(
                401,
                "AUTH_TOKEN_INVALID",
                "Console session is invalid or expired",
            )
        raise UserAuthError(
            401,
            "AUTH_TOKEN_MISSING",
            "Authentication token is required",
        )
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not request_origin_allowed(
        request, settings
    ):
        raise UserAuthError(
            403,
            "CONSOLE_CSRF_REJECTED",
            "Request origin is not allowed",
        )
    principal = console_principal(session)
    # The local console account has a stable username in the signed session;
    # keep that username (rather than the display label) in the audit record.
    request.state.audit_username = str(session["username"])
    return principal


def require_capability(*required: str) -> Callable:
    """Return a FastAPI dependency enforcing principal capabilities."""

    async def dependency(
        principal: UserPrincipal = Depends(require_console_or_user_principal),
    ) -> UserPrincipal:
        missing = [cap for cap in required if cap not in principal.capabilities]
        if missing and "admin" not in principal.capabilities:
            raise UserAuthError(
                403,
                "ACL_DENIED",
                "Access denied",
            )
        return principal

    return dependency


def _get_validator() -> JWTValidator:
    """Create a JWTValidator (reads env at construction)."""
    return JWTValidator()


def _get_repo() -> ExtUserMapRepo:
    """Create an ExtUserMapRepo backed by the app's PostgreSQL Gateway DB."""
    return ExtUserMapRepo()
