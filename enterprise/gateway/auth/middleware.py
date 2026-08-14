"""FastAPI dependencies for end-user and service authentication.

require_user_principal and require_service_principal are strictly separate.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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

    return principal


def require_capability(*required: str) -> Callable:
    """Return a FastAPI dependency enforcing principal capabilities."""

    async def dependency(
        principal: UserPrincipal = Depends(require_user_principal),
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
    """Create an ExtUserMapRepo (reads ENTERPRISE_DB_PATH from env at init)."""
    return ExtUserMapRepo()
