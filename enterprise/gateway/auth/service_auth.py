"""System-to-system authentication for legacy Bearer and v2 HMAC clients."""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from enterprise.gateway.auth.service_principal import ServicePrincipal

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)

HMAC_CREDENTIALS_ENV = "ENTERPRISE_SYNC_HMAC_CREDENTIALS"
TIMESTAMP_WINDOW_SECONDS = 300
REPLAY_RETENTION_SECONDS = 600
_VALID_CREDENTIAL_STATUSES = frozenset({"active", "previous", "revoked"})


class ReplayStoreUnavailable(RuntimeError):
    """The shared replay store could not be reached."""


class MemoryReplayStore:
    """Explicit test/development replay store; never selected in production."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    async def remember(self, key: str, now: float) -> bool:
        with self._lock:
            expired = [item for item, expiry in self._values.items() if expiry <= now]
            for item in expired:
                self._values.pop(item, None)
            if key in self._values:
                return False
            self._values[key] = now + REPLAY_RETENTION_SECONDS
            return True


def _resp_command(*parts: str) -> bytes:
    encoded = [part.encode("utf-8") for part in parts]
    return b"*" + str(len(encoded)).encode() + b"\r\n" + b"".join(
        b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
        for part in encoded
    )


async def _read_resp(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        raise ReplayStoreUnavailable("Replay store closed the connection")
    prefix, payload = line[:1], line[1:-2]
    if prefix == b"+":
        return payload.decode("utf-8", errors="replace")
    if prefix == b"-":
        raise ReplayStoreUnavailable(payload.decode("utf-8", errors="replace"))
    if prefix == b"$":
        length = int(payload)
        if length < 0:
            return None
        value = await reader.readexactly(length)
        await reader.readexactly(2)
        return value
    raise ReplayStoreUnavailable("Unexpected replay store response")


class RedisReplayStore:
    """Small dependency-free Redis/Valkey SET NX EX adapter."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "tyrag:service-replay:",
        timeout: float = 2.0,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("ENTERPRISE_REDIS_URL must be redis:// or rediss://")
        self.host = parsed.hostname
        self.port = parsed.port or 6379
        self.username = unquote(parsed.username) if parsed.username else None
        self.password = unquote(parsed.password) if parsed.password else None
        try:
            self.database = int((parsed.path or "/0").lstrip("/") or 0)
        except ValueError as exc:
            raise ValueError("Redis database must be an integer") from exc
        self.ssl = parsed.scheme == "rediss"
        self.prefix = prefix
        self.timeout = timeout

    async def remember(self, key: str, now: float) -> bool:
        del now
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.ssl),
                timeout=self.timeout,
            )
            try:
                if self.password is not None:
                    auth_args = ("AUTH", self.username, self.password) if self.username else (
                        "AUTH", self.password
                    )
                    writer.write(_resp_command(*auth_args))
                    await asyncio.wait_for(writer.drain(), timeout=self.timeout)
                    await asyncio.wait_for(_read_resp(reader), timeout=self.timeout)
                if self.database:
                    writer.write(_resp_command("SELECT", str(self.database)))
                    await asyncio.wait_for(writer.drain(), timeout=self.timeout)
                    await asyncio.wait_for(_read_resp(reader), timeout=self.timeout)
                writer.write(
                    _resp_command(
                        "SET",
                        self.prefix + key,
                        "1",
                        "NX",
                        "EX",
                        str(REPLAY_RETENTION_SECONDS),
                    )
                )
                await asyncio.wait_for(writer.drain(), timeout=self.timeout)
                return (
                    await asyncio.wait_for(_read_resp(reader), timeout=self.timeout)
                ) is not None
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=self.timeout)
                except Exception:
                    pass
        except Exception as exc:
            raise ReplayStoreUnavailable("Shared replay store unavailable") from exc


def _default_replay_store():
    configured = os.environ.get("ENTERPRISE_SERVICE_REPLAY_STORE", "").strip().lower()
    if not configured:
        configured = "memory" if os.environ.get("ENTERPRISE_TEST_MODE") == "1" else "redis"
    if configured == "memory":
        if os.environ.get("ENTERPRISE_TEST_MODE") != "1":
            raise ReplayStoreUnavailable(
                "Memory replay protection is only allowed in explicit test mode"
            )
        return MemoryReplayStore()
    if configured != "redis":
        raise ReplayStoreUnavailable("Unsupported service replay store")
    try:
        return RedisReplayStore(
            os.environ.get("ENTERPRISE_REDIS_URL", "redis://127.0.0.1:6379/0"),
            prefix=os.environ.get(
                "ENTERPRISE_REPLAY_KEY_PREFIX", "tyrag:service-replay:"
            ),
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReplayStoreUnavailable("Shared replay store is misconfigured") from exc


def _epoch(value: object | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        raise ValueError("credential time must be epoch seconds or RFC3339")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass(frozen=True, order=True)
class CredentialBinding:
    """One authorized tenant/source pair; pairs are never cross-combined."""

    tenant_id: str
    source_system: str

    def __post_init__(self) -> None:
        tenant_id = self.tenant_id.strip()
        source_system = self.source_system.strip()
        if not tenant_id or not source_system:
            raise ValueError("credential binding values must not be empty")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "source_system", source_system)


@dataclass(frozen=True)
class CredentialIdentity:
    """Server-side HMAC key identity and its exact authorization bindings.

    A ``previous`` key is accepted only through ``valid_until``, which is its
    rotation grace deadline. A ``revoked`` key is never accepted.
    """

    credential_id: str
    key_id: str
    secret: str = field(repr=False)
    allowed_bindings: frozenset[CredentialBinding] = field(default_factory=frozenset)
    status: str = "active"
    valid_from: float | datetime | str | None = None
    valid_until: float | datetime | str | None = None

    def __post_init__(self) -> None:
        credential_id = self.credential_id.strip()
        key_id = self.key_id.strip()
        status = self.status.strip().lower()
        if not credential_id or not key_id or not self.secret:
            raise ValueError("credential_id, key_id and secret are required")
        if status not in _VALID_CREDENTIAL_STATUSES:
            raise ValueError("unsupported credential status")
        bindings = frozenset(self.allowed_bindings)
        if not bindings:
            raise ValueError("at least one allowed binding is required")
        valid_from = _epoch(self.valid_from)
        valid_until = _epoch(self.valid_until)
        if valid_from is not None and valid_until is not None:
            if valid_from > valid_until:
                raise ValueError("valid_from must not be later than valid_until")
        if status == "previous" and valid_until is None:
            raise ValueError("previous credential requires a grace deadline")
        object.__setattr__(self, "credential_id", credential_id)
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "allowed_bindings", bindings)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)

    def is_usable_at(self, now: float) -> bool:
        if self.status == "revoked":
            return False
        if self.valid_from is not None and now < self.valid_from:
            return False
        if self.valid_until is not None and now > self.valid_until:
            return False
        return self.status in {"active", "previous"}


def canonical_path_query(path: str, query: str | bytes = "") -> str:
    """Return an RFC3986-normalized path with a stably sorted query."""
    if isinstance(query, bytes):
        query = query.decode("ascii", errors="strict")
    normalized_path = quote(unquote(path or "/"), safe="/-._~")
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    encoded_pairs = sorted(
        (
            quote(key, safe="-._~"),
            quote(value, safe="-._~"),
        )
        for key, value in pairs
    )
    if not encoded_pairs:
        return normalized_path
    canonical_query = "&".join(f"{key}={value}" for key, value in encoded_pairs)
    return f"{normalized_path}?{canonical_query}"


def canonical_request(
    *,
    timestamp: str,
    method: str,
    path: str,
    query: str | bytes,
    body: bytes,
) -> bytes:
    """Build the v1 canonical signing input using the raw body digest."""
    body_hash = hashlib.sha256(body).hexdigest()
    target = canonical_path_query(path, query)
    return (
        f"v1\n{timestamp}\n{method.upper()}\n{target}\n{body_hash}"
    ).encode("utf-8")


def sign_request(
    *,
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    query: str | bytes = "",
    body: bytes = b"",
) -> str:
    """Create the wire value for ``X-TY-Signature``."""
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_request(
            timestamp=timestamp,
            method=method,
            path=path,
            query=query,
            body=body,
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def _credential_from_dict(value: object) -> CredentialIdentity:
    if not isinstance(value, dict):
        raise ValueError("credential entry must be an object")
    raw_bindings = value.get("allowedBindings", value.get("allowed_bindings"))
    if not isinstance(raw_bindings, list):
        raise ValueError("allowedBindings must be an array")
    bindings: set[CredentialBinding] = set()
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            raise ValueError("allowedBindings entry must be an object")
        bindings.add(
            CredentialBinding(
                tenant_id=str(raw.get("tenantId", raw.get("tenant_id", ""))),
                source_system=str(
                    raw.get("sourceSystem", raw.get("source_system", ""))
                ),
            )
        )
    return CredentialIdentity(
        credential_id=str(
            value.get("credentialId", value.get("credential_id", ""))
        ),
        key_id=str(value.get("keyId", value.get("key_id", ""))),
        secret=str(value.get("secret", "")),
        allowed_bindings=frozenset(bindings),
        status=str(value.get("status", "active")),
        valid_from=value.get("validFrom", value.get("valid_from")),
        valid_until=value.get("validUntil", value.get("valid_until")),
    )


def _validate_identities(
    identities: Iterable[CredentialIdentity],
) -> tuple[CredentialIdentity, ...]:
    validated = tuple(identities)
    key_ids = [identity.key_id for identity in validated]
    if len(key_ids) != len(set(key_ids)):
        raise ValueError("keyId values must be unique")
    active_credentials = [
        identity.credential_id
        for identity in validated
        if identity.status == "active"
    ]
    if len(active_credentials) != len(set(active_credentials)):
        raise ValueError("each credentialId may have only one active key")
    return validated


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


class ServiceAuthenticator:
    """Authenticate legacy Bearer tokens or signed v2 service requests."""

    def __init__(
        self,
        identities: Iterable[CredentialIdentity] | None = None,
        replay_store=None,
    ) -> None:
        self._configured_identities = (
            None if identities is None else _validate_identities(identities)
        )
        self._replay_cache: dict[str, float] = {}
        self._replay_lock = threading.Lock()
        self._replay_store = replay_store

    @property
    def _enabled(self) -> bool:
        return os.environ.get(
            "ENTERPRISE_SYNC_AUTH_ENABLED", "true"
        ).lower() == "true"

    @property
    def _token(self) -> str:
        return os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")

    @property
    def identities(self) -> tuple[CredentialIdentity, ...]:
        if self._configured_identities is not None:
            return self._configured_identities
        raw = os.environ.get(HMAC_CREDENTIALS_ENV, "").strip()
        if not raw:
            return ()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = parsed.get("credentials")
            if not isinstance(parsed, list):
                raise ValueError("credential configuration must be an array")
            return _validate_identities(
                _credential_from_dict(item) for item in parsed
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("Invalid server-side HMAC credential configuration")
            return ()

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
        """Validate the legacy bearer token and return a principal."""
        if not self._enabled:
            return ServicePrincipal(source_system="anonymous", authenticated=False)

        if credentials is None or not credentials.credentials:
            raise _http_error(401, "AUTH_TOKEN_MISSING", "Service token required")

        if not self.verify(credentials.credentials):
            logger.warning(
                "Service authentication failed: invalid token (length=%d)",
                len(credentials.credentials),
            )
            raise _http_error(401, "AUTH_TOKEN_INVALID", "Invalid service token")

        return ServicePrincipal(source_system="service")

    def _identity_for_key(self, key_id: str) -> CredentialIdentity | None:
        matches = [item for item in self.identities if item.key_id == key_id]
        return matches[0] if len(matches) == 1 else None

    def _remember_signature(self, replay_key: str, now: float) -> bool:
        """Return false for a replay; accepted keys remain for ten minutes."""
        with self._replay_lock:
            expired = [
                key for key, expires_at in self._replay_cache.items()
                if expires_at <= now
            ]
            for key in expired:
                self._replay_cache.pop(key, None)
            if replay_key in self._replay_cache:
                return False
            self._replay_cache[replay_key] = now + REPLAY_RETENTION_SECONDS
            return True

    async def _remember_replay(self, replay_key: str, now: float) -> bool:
        """Atomically reserve a replay key in the configured shared store."""
        store = self._replay_store
        if store is None:
            store = _default_replay_store()
            self._replay_store = store
        if isinstance(store, MemoryReplayStore):
            return self._remember_signature(replay_key, now)
        try:
            return await store.remember(replay_key, now)
        except ReplayStoreUnavailable:
            raise
        except Exception as exc:
            raise ReplayStoreUnavailable("Shared replay store unavailable") from exc

    @staticmethod
    async def _request_binding(
        request: Request, body: bytes
    ) -> CredentialBinding:
        tenant_values: list[str] = []
        source_values: list[str] = []

        for name in ("tenantId", "tenant_id"):
            tenant_values.extend(request.query_params.getlist(name))
        for name in ("sourceSystem", "source_system"):
            source_values.extend(request.query_params.getlist(name))

        if body:
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                metadata = payload.get("metadata")
                objects = [payload]
                if isinstance(metadata, dict):
                    objects.append(metadata)
                for item in objects:
                    for name in ("tenantId", "tenant_id"):
                        value = item.get(name)
                        if value is not None:
                            tenant_values.append(str(value))
                    for name in ("sourceSystem", "source_system"):
                        value = item.get(name)
                        if value is not None:
                            source_values.append(str(value))

        tenants = {value.strip() for value in tenant_values if value.strip()}
        sources = {value.strip() for value in source_values if value.strip()}
        if len(tenants) > 1 or len(sources) > 1:
            raise _http_error(
                403,
                "AUTH_BINDING_CONFLICT",
                "Conflicting credential binding values",
            )
        if len(tenants) != 1 or len(sources) != 1:
            raise _http_error(
                403,
                "AUTH_BINDING_MISSING",
                "Tenant and source system are required",
            )
        return CredentialBinding(tenants.pop(), sources.pop())

    async def authenticate_request(
        self,
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials],
        *,
        now: float | None = None,
    ) -> ServicePrincipal:
        """Authenticate one request, preferring legacy Bearer when supplied."""
        if not self._enabled or credentials is not None:
            return self.authenticate(credentials)

        timestamp = request.headers.get("X-TY-Timestamp", "").strip()
        key_id = request.headers.get("X-TY-Key-Id", "").strip()
        signature = request.headers.get("X-TY-Signature", "").strip().lower()
        if not timestamp and not key_id and not signature:
            return self.authenticate(None)
        if not timestamp or not key_id or not signature:
            raise _http_error(
                401, "AUTH_SIGNATURE_MISSING", "HMAC signature headers required"
            )
        try:
            request_time = int(timestamp)
        except ValueError:
            raise _http_error(
                401, "AUTH_TIMESTAMP_INVALID", "Invalid request timestamp"
            ) from None
        current_time = time.time() if now is None else float(now)
        if abs(current_time - request_time) > TIMESTAMP_WINDOW_SECONDS:
            raise _http_error(
                401, "AUTH_TIMESTAMP_INVALID", "Invalid request timestamp"
            )

        identity = self._identity_for_key(key_id)
        if identity is None:
            raise _http_error(
                401, "AUTH_SIGNATURE_INVALID", "Invalid request signature"
            )
        if not identity.is_usable_at(current_time):
            raise _http_error(
                401, "AUTH_CREDENTIAL_INACTIVE", "Credential is not active"
            )

        body = await request.body()
        expected = sign_request(
            secret=identity.secret,
            timestamp=timestamp,
            method=request.method,
            path=request.scope.get("raw_path", request.url.path.encode()).decode(
                "ascii", errors="strict"
            ),
            query=request.scope.get("query_string", b""),
            body=body,
        )
        if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
            raise _http_error(
                401, "AUTH_SIGNATURE_INVALID", "Invalid request signature"
            )

        replay_key = hashlib.sha256(
            f"{identity.credential_id}\n{key_id}\n{timestamp}\n{signature}".encode(
                "utf-8"
            )
        ).hexdigest()
        try:
            accepted = await self._remember_replay(replay_key, current_time)
        except ReplayStoreUnavailable:
            logger.error("Shared service replay store unavailable; failing closed")
            raise _http_error(
                503,
                "AUTH_REPLAY_STORE_UNAVAILABLE",
                "Replay protection store is unavailable",
            ) from None
        if not accepted:
            raise _http_error(
                401, "AUTH_REPLAY_DETECTED", "Request replay detected"
            )

        binding = await self._request_binding(request, body)
        if binding not in identity.allowed_bindings:
            raise _http_error(
                403, "AUTH_BINDING_DENIED", "Credential binding is not allowed"
            )
        return ServicePrincipal(
            source_system=binding.source_system,
            credential_id=identity.credential_id,
            key_id=identity.key_id,
            allowed_bindings=frozenset(
                (item.tenant_id, item.source_system)
                for item in identity.allowed_bindings
            ),
        )


_service_auth = ServiceAuthenticator()


async def require_service_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> ServicePrincipal:
    """FastAPI dependency: enforce service-level authentication."""
    return await _service_auth.authenticate_request(request, credentials)
