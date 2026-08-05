"""JWT TokenValidator with configurable claim mapping and JWKS support.

Validates end-user JWT tokens from customer identity system.
Separate from ServiceAuthenticator (shared bearer token for WP-02A).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient, PyJWKClientError

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_ALGS = frozenset({
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
    "EdDSA",
})

_HMAC_ALGS = frozenset({"HS256", "HS384", "HS512"})


class TokenValidationError(Exception):
    """Stable, safe authentication error.  Message never contains token material."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class JWTValidatorConfig:
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""
    allowed_algorithms: tuple[str, ...] = ("RS256", "ES256")
    enable_hs_algorithms: bool = False
    claim_map: dict[str, str] = field(default_factory=dict)
    jwks_cache_ttl: int = 300
    jwks_timeout: float = 5.0

    @classmethod
    def from_env(cls) -> "JWTValidatorConfig":
        raw_claim_map = os.environ.get("JWT_CLAIM_MAP", "")
        claim_map = {}
        if raw_claim_map:
            try:
                claim_map = json.loads(raw_claim_map)
            except json.JSONDecodeError:
                logger.warning("JWT_CLAIM_MAP is not valid JSON; using defaults")
        if not claim_map:
            claim_map = {
                "sub": "sub",
                "tenant_id": "tenant",
                "business_user_id": "business_user_id",
                "display_name": "name",
                "department_ids": "department",
                "role_codes": "roles",
                "group_ids": "groups",
                "security_level": "security_level",
            }
        raw_algs = os.environ.get("JWT_ALLOWED_ALGS", "RS256,ES256")
        algs = tuple(a.strip() for a in raw_algs.split(",") if a.strip())
        return cls(
            issuer=os.environ.get("JWT_ISSUER", ""),
            audience=os.environ.get("JWT_AUDIENCE", ""),
            jwks_url=os.environ.get("JWT_JWKS_URL", ""),
            allowed_algorithms=algs or ("RS256", "ES256"),
            enable_hs_algorithms=os.environ.get("JWT_ENABLE_HS", "").lower() == "true",
            claim_map=claim_map,
            jwks_cache_ttl=int(os.environ.get("JWT_JWKS_CACHE_TTL", "300")),
            jwks_timeout=float(os.environ.get("JWT_JWKS_TIMEOUT", "5.0")),
        )


class JWTValidator:
    """Validates end-user JWT tokens. Does NOT share keys with ServiceAuthenticator."""

    def __init__(self, config: JWTValidatorConfig | None = None):
        self._config = config or JWTValidatorConfig.from_env()
        self._jwks_client: PyJWKClient | None = None
        self._jwks_last_refresh: float = 0.0

    @property
    def allowed_algorithms(self) -> list[str]:
        algs = set(self._config.allowed_algorithms)
        if not self._config.enable_hs_algorithms:
            algs -= _HMAC_ALGS
        valid = algs & (DEFAULT_ALLOWED_ALGS | _HMAC_ALGS)
        if not valid:
            raise TokenValidationError("CONFIG_ERROR", "No valid JWT algorithms configured")
        return sorted(valid)

    def _get_jwks_client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwks_client is None or now - self._jwks_last_refresh > self._config.jwks_cache_ttl:
            self._jwks_client = PyJWKClient(
                self._config.jwks_url,
                cache_keys=True,
                lifespan=self._config.jwks_cache_ttl,
                timeout=self._config.jwks_timeout,
            )
            self._jwks_last_refresh = now
        return self._jwks_client

    def validate(self, token: str) -> dict[str, Any]:
        """Validate a JWT and return verified claims. Raises TokenValidationError on failure."""
        if not token or not token.strip():
            raise TokenValidationError("AUTH_TOKEN_MISSING", "Authentication token is required")
        if not self._config.issuer:
            raise TokenValidationError("CONFIG_ERROR", "JWT issuer not configured")

        algorithms = self.allowed_algorithms
        try:
            unverified_headers = jwt.get_unverified_header(token)
        except jwt.DecodeError as e:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Unable to parse token header") from e

        alg = unverified_headers.get("alg", "")
        if alg in _HMAC_ALGS and not self._config.enable_hs_algorithms:
            raise TokenValidationError("AUTH_TOKEN_INVALID", f"Algorithm '{alg}' is not allowed")

        if self._config.jwks_url:
            try:
                jwks_client = self._get_jwks_client()
                signing_key = jwks_client.get_signing_key_from_jwt(token).key
            except PyJWKClientError as e:
                logger.error("JWKS fetch failed: %s", e)
                raise TokenValidationError("AUTH_TOKEN_INVALID", "Unable to verify token signature") from e
            except Exception as e:
                logger.error("JWKS key lookup failed: %s", e)
                raise TokenValidationError("AUTH_TOKEN_INVALID", "Token signing key not found") from e
        else:
            if not self._config.enable_hs_algorithms:
                raise TokenValidationError("CONFIG_ERROR", "JWT JWKS URL not configured and HS algorithms not enabled")
            signing_key = os.environ.get("JWT_SHARED_SECRET", "")
            if not signing_key:
                raise TokenValidationError("CONFIG_ERROR", "JWT shared secret not configured")

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=algorithms,
                issuer=self._config.issuer,
                audience=self._config.audience or None,
                options={
                    "require": ["exp", "sub"],
                    "verify_exp": True,
                    "verify_nbf": True,
                },
            )
        except jwt.ExpiredSignatureError as e:
            raise TokenValidationError("AUTH_TOKEN_EXPIRED", "Token has expired") from e
        except jwt.ImmatureSignatureError as e:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Token is not yet valid (nbf)") from e
        except jwt.InvalidIssuerError as e:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Invalid token issuer") from e
        except jwt.InvalidAudienceError as e:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Invalid token audience") from e
        except jwt.PyJWTError as e:
            logger.warning("JWT validation failed: %s", e)
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Token validation failed") from e

        sub = claims.get("sub", "")
        if not sub:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Token missing subject claim")
        tenant = claims.get(self._config.claim_map.get("tenant_id", "tenant"), "")
        if not tenant:
            raise TokenValidationError("AUTH_TOKEN_INVALID", "Token missing tenant claim")

        logger.debug("JWT validated: sub=%s tenant=%s", sub, tenant)
        return claims
