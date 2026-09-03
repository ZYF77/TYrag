"""Local, single-account session support for the Gateway WebUI.

This module is deliberately separate from EAM JWT validation.  The local
operator account is configured with a password hash and a session signing
secret; no credential material is persisted in the Gateway database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from enterprise.gateway.auth.user_principal import UserPrincipal

CONSOLE_SESSION_COOKIE = "enterprise_console_session"
CONSOLE_SESSION_COOKIE_PATH = "/enterprise/api"
CONSOLE_SESSION_AUDIENCE = "enterprise-gateway-console"
CONSOLE_USERNAME = "zkadmin"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SESSION_VERSION = 1
_CONSOLE_CAPABILITIES = (
    "admin",
    "ask",
    "audit",
    "list_sessions",
    "manage_metadata",
    "read",
    "review",
    "upload",
    "view_citations",
)
_CONSOLE_ROLES = (
    "auditor",
    "end_user",
    "knowledge_maintainer",
    "system_admin",
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or len(value) > 4096:
        raise ValueError("invalid encoded value")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def hash_console_password(password: str) -> str:
    """Create a versioned scrypt password hash for operator provisioning.

    The resulting value is safe to place in a secret environment variable,
    but must not be committed alongside application code.
    """

    if not isinstance(password, str) or not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    # Use colon separators so the value can be copied into a Docker Compose
    # environment file without `$` interpolation changing the hash.
    return ":".join(
        (
            "scrypt",
            "v1",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_console_password(password: str, encoded: str) -> bool:
    """Verify a provisioned scrypt password hash in constant time."""

    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        parts = encoded.split(":")
        if len(parts) != 7 or parts[:2] != ["scrypt", "v1"]:
            return False
        n, r, p = (int(parts[index]) for index in (2, 3, 4))
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = _b64decode(parts[5])
        expected = _b64decode(parts[6])
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (TypeError, ValueError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ConsoleAuthSettings:
    """Process configuration for the single local WebUI operator account."""

    username: str
    password_hash: str
    session_secret: str
    tenant_id: str
    session_ttl_seconds: int
    cookie_secure: bool
    enabled: bool

    @classmethod
    def from_env(cls) -> "ConsoleAuthSettings":
        return cls(
            username=CONSOLE_USERNAME,
            password_hash=os.getenv("ENTERPRISE_CONSOLE_PASSWORD_HASH", "").strip(),
            session_secret=os.getenv("ENTERPRISE_CONSOLE_SESSION_SECRET", "").strip(),
            tenant_id=os.getenv("ENTERPRISE_CONSOLE_TENANT_ID", "").strip(),
            session_ttl_seconds=_env_int(
                "ENTERPRISE_CONSOLE_SESSION_TTL_SECONDS",
                8 * 60 * 60,
                15 * 60,
                24 * 60 * 60,
            ),
            # The local deployment currently uses HTTP on port 3000.  Set this
            # to true when TLS terminates in front of the WebUI.
            cookie_secure=_env_bool("ENTERPRISE_CONSOLE_COOKIE_SECURE", False),
            enabled=_env_bool("ENTERPRISE_CONSOLE_AUTH_ENABLED", True),
        )

    @property
    def configured(self) -> bool:
        values_ready = all(
            value and not value.startswith("REPLACE_WITH_")
            for value in (self.password_hash, self.session_secret, self.tenant_id)
        )
        return bool(
            self.enabled
            and values_ready
            and len(self.session_secret) >= 32
            and self.tenant_id
            and self.username
        )


def _session_payload(
    *, username: str, tenant_id: str, issued_at: int, expires_at: int
) -> bytes:
    return json.dumps(
        {
            "v": _SESSION_VERSION,
            "aud": CONSOLE_SESSION_AUDIENCE,
            "sub": username,
            "tenant": tenant_id,
            "iat": issued_at,
            "exp": expires_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def create_console_session(
    settings: ConsoleAuthSettings,
    *,
    now: int | None = None,
) -> tuple[str, int]:
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + settings.session_ttl_seconds
    payload = _b64encode(
        _session_payload(
            username=settings.username,
            tenant_id=settings.tenant_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    )
    signature = hmac.new(
        settings.session_secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload}.{_b64encode(signature)}", expires_at


def verify_console_session(
    value: str,
    settings: ConsoleAuthSettings,
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    if not settings.configured or not value or len(value) > 8192:
        return None
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload_bytes = _b64decode(encoded_payload)
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, TypeError):
        return None
    expected_signature = hmac.new(
        settings.session_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        username = str(payload["sub"])
        tenant_id = str(payload["tenant"])
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None
    current = int(time.time() if now is None else now)
    if payload.get("v") != _SESSION_VERSION:
        return None
    if payload.get("aud") != CONSOLE_SESSION_AUDIENCE:
        return None
    if username != settings.username or tenant_id != settings.tenant_id:
        return None
    if issued_at > current + 60 or expires_at <= current or expires_at <= issued_at:
        return None
    if expires_at - issued_at > settings.session_ttl_seconds + 60:
        return None
    return {
        "username": username,
        "tenantId": tenant_id,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
    }


def console_principal(session: dict[str, Any]) -> UserPrincipal:
    """Build the fixed, full-capability Principal used by WebUI requests."""

    username = str(session["username"])
    tenant_id = str(session["tenantId"])
    return UserPrincipal(
        tenant_id=tenant_id,
        business_user_id=username,
        subject=f"console:{username}",
        display_name="Gateway 运维管理员",
        role_codes=_CONSOLE_ROLES,
        mapping_status="active",
        token_issued_at=int(session["issuedAt"]),
        token_expires_at=int(session["expiresAt"]),
        capabilities=_CONSOLE_CAPABILITIES,
    )


def session_public_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": str(session["username"]),
        "tenantId": str(session["tenantId"]),
        "expiresAt": datetime_from_epoch(int(session["expiresAt"])),
    }


def datetime_from_epoch(value: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


class FailedLoginLimiter:
    """Small process-local limiter suitable for the single WebUI account."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 5 * 60,
        lockout_seconds: int = 15 * 60,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def is_blocked(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        locked_until = self._locked_until.get(key, 0.0)
        if locked_until > current:
            return True
        if locked_until:
            self._locked_until.pop(key, None)
        failures = [
            item
            for item in self._failures.get(key, [])
            if item > current - self.window_seconds
        ]
        if failures:
            self._failures[key] = failures
        else:
            self._failures.pop(key, None)
        return False

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        failures = [
            item
            for item in self._failures.get(key, [])
            if item > current - self.window_seconds
        ]
        failures.append(current)
        self._failures[key] = failures
        if len(failures) >= self.max_attempts:
            self._locked_until[key] = current + self.lockout_seconds

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


def request_origin_allowed(request: Any, settings: ConsoleAuthSettings) -> bool:
    """Allow same-origin WebUI requests and explicitly configured origins."""

    origin = request.headers.get("origin")
    if not origin:
        return True
    configured = {
        item.strip().rstrip("/")
        for item in os.getenv("ENTERPRISE_CONSOLE_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin.rstrip("/") in configured:
        return True
    forwarded_proto = request.headers.get(
        "x-forwarded-proto", ""
    ).split(",", 1)[0].strip()
    scheme = forwarded_proto or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return origin.rstrip("/") == f"{scheme}://{host}".rstrip("/")


def console_session_from_request(
    request: Any,
    settings: ConsoleAuthSettings | None = None,
) -> dict[str, Any] | None:
    current = settings or ConsoleAuthSettings.from_env()
    value = request.cookies.get(CONSOLE_SESSION_COOKIE)
    if not value:
        return None
    return verify_console_session(value, current)


__all__ = [
    "CONSOLE_SESSION_COOKIE",
    "CONSOLE_SESSION_COOKIE_PATH",
    "ConsoleAuthSettings",
    "FailedLoginLimiter",
    "console_principal",
    "console_session_from_request",
    "create_console_session",
    "hash_console_password",
    "request_origin_allowed",
    "session_public_payload",
    "verify_console_password",
    "verify_console_session",
]
