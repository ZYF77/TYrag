"""FastAPI dependencies for end-user and service authentication.

require_user_principal and require_service_principal are strictly separate.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from enterprise.gateway.auth.token_validator import JWTValidator, TokenValidationError
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.models.ext_user_map import ExtUserMapRepo

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


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
        raise UserAuthError(401, e.code, str(e)) from e

    principal = UserPrincipal.from_validated_claims(
        claims,
        validator._config.claim_map,
    )

    repo = _get_repo()
    try:
        await repo.ensure_table()
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
                "AUTH_USER_MAPPING_MISSING",
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

def _get_validator() -> JWTValidator:
    """Create a JWTValidator (reads env at construction)."""
    return JWTValidator()


def _get_repo() -> ExtUserMapRepo:
    """Create an ExtUserMapRepo (reads ENTERPRISE_DB_PATH from env at init)."""
    return ExtUserMapRepo()
