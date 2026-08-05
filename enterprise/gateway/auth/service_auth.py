"""Minimal system-to-system service authentication.

Uses a shared bearer token from environment. NOT for end-user auth.
Designed to be replaced later without touching endpoint code.
"""
import hashlib
import hmac
import logging
import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from enterprise.gateway.auth.service_principal import ServicePrincipal

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


class ServiceAuthenticator:
    """Validates a service bearer token against the configured secret.

    Uses constant-time comparison to avoid timing side-channels.
    The token and enabled flag are read lazily from env on each call,
    so tests can override them as instance attributes.
    """

    def __init__(self) -> None:
        pass

    @property
    def _enabled(self) -> bool:
        return os.environ.get(
            "ENTERPRISE_SYNC_AUTH_ENABLED", "true"
        ).lower() == "true"

    @property
    def _token(self) -> str:
        return os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")

    def verify(self, token: str) -> bool:
        if not self._enabled:
            return True
        if not self._token or not token:
            return False
        return hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(),
            hashlib.sha256(self._token.encode()).digest(),
        )

    def authenticate(
        self, credentials: Optional[HTTPAuthorizationCredentials]
    ) -> ServicePrincipal:
        """Validate the bearer token and return a ServicePrincipal.

        Raises HTTPException on failure so FastAPI returns 401.
        """
        if not self._enabled:
            return ServicePrincipal(source_system="anonymous", authenticated=False)

        if credentials is None or not credentials.credentials:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "AUTH_TOKEN_MISSING",
                    "message": "Service token required",
                },
            )

        if not self.verify(credentials.credentials):
            logger.warning(
                "Service authentication failed: invalid token "
                "(length=%d)", len(credentials.credentials)
            )
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "AUTH_TOKEN_INVALID",
                    "message": "Invalid service token",
                },
            )

        return ServicePrincipal(source_system="service")


_service_auth = ServiceAuthenticator()


async def require_service_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> ServicePrincipal:
    """FastAPI dependency: enforce service-level authentication."""
    return _service_auth.authenticate(credentials)
