"""P0 v2 service-auth contract tests."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import pytest
import enterprise.gateway.auth.service_auth as service_auth_module
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from enterprise.gateway.auth.service_auth import (
    CredentialBinding,
    CredentialIdentity,
    MemoryReplayStore,
    RedisReplayStore,
    ServiceAuthenticator,
    canonical_request,
    sign_request,
)


NOW = 1_800_000_000
SECRET = "test-hmac-secret"


@pytest.fixture(autouse=True)
def _enable_service_auth(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")


def _identity(
    *,
    key_id: str = "key-active",
    status: str = "active",
    valid_from: int | None = NOW - 60,
    valid_until: int | None = NOW + 3600,
) -> CredentialIdentity:
    return CredentialIdentity(
        credential_id="device-sync",
        key_id=key_id,
        secret=SECRET,
        allowed_bindings=frozenset(
            {CredentialBinding(tenant_id="tenant-a", source_system="EAM")}
        ),
        status=status,
        valid_from=valid_from,
        valid_until=valid_until,
    )


def _request(
    *,
    body: bytes,
    timestamp: int = NOW,
    key_id: str = "key-active",
    secret: str = SECRET,
    path: str = "/enterprise/api/v1/documents",
    query: str = "",
    method: str = "POST",
) -> Request:
    signature = sign_request(
        secret=secret,
        timestamp=str(timestamp),
        method=method,
        path=path,
        query=query,
        body=body,
    )
    headers = [
        (b"content-type", b"application/json"),
        (b"x-ty-timestamp", str(timestamp).encode("ascii")),
        (b"x-ty-key-id", key_id.encode("ascii")),
        (b"x-ty-signature", signature.encode("ascii")),
    ]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("test", 443),
        },
        receive,
    )


def _body(
    *, tenant_id: str = "tenant-a", source_system: str = "EAM"
) -> bytes:
    return json.dumps(
        {
            "sourceSystem": source_system,
            "metadata": {
                "tenant_id": tenant_id,
                "source_system": source_system,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_active_hmac_authenticates_bound_identity():
    auth = ServiceAuthenticator(identities=[_identity()])

    principal = await auth.authenticate_request(
        _request(body=_body()), credentials=None, now=NOW
    )

    assert principal.authenticated is True
    assert principal.credential_id == "device-sync"
    assert principal.key_id == "key-active"
    assert principal.source_system == "EAM"


@pytest.mark.asyncio
async def test_allowed_bindings_are_pairs_not_cartesian_lists():
    identity = CredentialIdentity(
        credential_id="device-sync",
        key_id="key-active",
        secret=SECRET,
        allowed_bindings=frozenset(
            {
                CredentialBinding("tenant-a", "EAM"),
                CredentialBinding("tenant-b", "PLM"),
            }
        ),
        status="active",
        valid_from=NOW - 60,
        valid_until=NOW + 60,
    )
    auth = ServiceAuthenticator(identities=[identity])

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(
            _request(body=_body(tenant_id="tenant-a", source_system="PLM")),
            credentials=None,
            now=NOW,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "AUTH_BINDING_DENIED"


@pytest.mark.asyncio
async def test_body_and_query_binding_conflict_is_rejected():
    auth = ServiceAuthenticator(identities=[_identity()])
    request = _request(
        body=_body(),
        query="tenant_id=tenant-b&source_system=EAM",
    )

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(request, credentials=None, now=NOW)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "AUTH_BINDING_CONFLICT"


def test_signature_covers_sorted_query_and_raw_body_hash():
    body = _body()
    canonical = canonical_request(
        timestamp=str(NOW),
        method="post",
        path="/enterprise/api/v1/documents",
        query="z=last&a=two%20words&a=first",
        body=body,
    ).decode("utf-8")

    assert canonical.splitlines() == [
        "v1",
        str(NOW),
        "POST",
        "/enterprise/api/v1/documents?a=first&a=two%20words&z=last",
        hashlib.sha256(body).hexdigest(),
    ]


@pytest.mark.asyncio
async def test_tampered_body_is_rejected():
    auth = ServiceAuthenticator(identities=[_identity()])
    signed = _request(body=_body())

    async def tampered_receive():
        return {
            "type": "http.request",
            "body": _body(tenant_id="tenant-b"),
            "more_body": False,
        }

    tampered = Request(signed.scope, tampered_receive)

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(tampered, credentials=None, now=NOW)

    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTH_SIGNATURE_INVALID"


@pytest.mark.asyncio
async def test_hmac_digest_uses_constant_time_compare(monkeypatch):
    compared = False
    original = service_auth_module.hmac.compare_digest

    def compare_digest(left, right):
        nonlocal compared
        compared = True
        return original(left, right)

    monkeypatch.setattr(service_auth_module.hmac, "compare_digest", compare_digest)
    auth = ServiceAuthenticator(identities=[_identity()])

    await auth.authenticate_request(
        _request(body=_body()), credentials=None, now=NOW
    )

    assert compared is True


@pytest.mark.asyncio
async def test_timestamp_window_and_ten_minute_replay_cache():
    auth = ServiceAuthenticator(identities=[_identity()])
    body = _body()

    stale = _request(body=body, timestamp=NOW - 301)
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(stale, credentials=None, now=NOW)
    assert exc.value.detail["code"] == "AUTH_TIMESTAMP_INVALID"

    request = _request(body=body)
    await auth.authenticate_request(request, credentials=None, now=NOW)
    replay = _request(body=body)
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(replay, credentials=None, now=NOW + 299)
    assert exc.value.detail["code"] == "AUTH_REPLAY_DETECTED"

    replay_key = next(iter(auth._replay_cache))
    assert auth._remember_signature(replay_key, NOW + 601) is True


@pytest.mark.asyncio
async def test_explicit_memory_fixture_reserves_across_auth_instances():
    store = MemoryReplayStore()
    first = ServiceAuthenticator(identities=[_identity()], replay_store=store)
    second = ServiceAuthenticator(identities=[_identity()], replay_store=store)

    await first.authenticate_request(_request(body=_body()), credentials=None, now=NOW)
    with pytest.raises(HTTPException) as exc:
        await second.authenticate_request(
            _request(body=_body()), credentials=None, now=NOW
        )
    assert exc.value.detail["code"] == "AUTH_REPLAY_DETECTED"


@pytest.mark.asyncio
async def test_production_replay_store_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.setenv("ENTERPRISE_SERVICE_REPLAY_STORE", "redis")
    monkeypatch.setenv("ENTERPRISE_REDIS_URL", "redis://127.0.0.1:1/0")
    auth = ServiceAuthenticator(identities=[_identity()])

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(_request(body=_body()), credentials=None, now=NOW)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "AUTH_REPLAY_STORE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_malformed_production_replay_store_fails_closed(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.setenv("ENTERPRISE_SERVICE_REPLAY_STORE", "redis")
    monkeypatch.setenv("ENTERPRISE_REDIS_URL", "http://not-redis")
    auth = ServiceAuthenticator(identities=[_identity()])

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(
            _request(body=_body()), credentials=None, now=NOW
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "AUTH_REPLAY_STORE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_previous_key_grace_and_revocation():
    previous = _identity(
        key_id="key-previous",
        status="previous",
        valid_until=NOW + 30,
    )
    auth = ServiceAuthenticator(identities=[previous])
    principal = await auth.authenticate_request(
        _request(body=_body(), key_id="key-previous"),
        credentials=None,
        now=NOW,
    )
    assert principal.key_id == "key-previous"

    expired_auth = ServiceAuthenticator(identities=[previous])
    with pytest.raises(HTTPException) as exc:
        await expired_auth.authenticate_request(
            _request(body=_body(), key_id="key-previous", timestamp=NOW + 31),
            credentials=None,
            now=NOW + 31,
        )
    assert exc.value.detail["code"] == "AUTH_CREDENTIAL_INACTIVE"

    revoked = _identity(status="revoked")
    revoked_auth = ServiceAuthenticator(identities=[revoked])
    with pytest.raises(HTTPException) as exc:
        await revoked_auth.authenticate_request(
            _request(body=_body()), credentials=None, now=NOW
        )
    assert exc.value.detail["code"] == "AUTH_KEY_REVOKED"


@pytest.mark.asyncio
async def test_credential_expiry_is_exclusive_and_old_key_id_is_rejected():
    expired = _identity(valid_until=NOW)
    with pytest.raises(HTTPException) as exc:
        await ServiceAuthenticator(identities=[expired]).authenticate_request(
            _request(body=_body()), credentials=None, now=NOW
        )
    assert exc.value.detail["code"] == "AUTH_CREDENTIAL_INACTIVE"

    auth = ServiceAuthenticator(identities=[_identity()])
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_request(
            _request(body=_body(), key_id="key-old"), credentials=None, now=NOW
        )
    assert exc.value.detail["code"] == "AUTH_SIGNATURE_INVALID"


@pytest.mark.asyncio
async def test_invalid_timestamp_and_signature_wire_values_are_rejected():
    request = _request(body=_body())

    async def receive():
        return {"type": "http.request", "body": _body(), "more_body": False}

    invalid = Request(
        {
            **request.scope,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-ty-timestamp", b"+1800000000"),
                (b"x-ty-key-id", b"key-active"),
                (b"x-ty-signature", b"v1=" + b"0" * 64),
            ],
        },
        receive,
    )
    with pytest.raises(HTTPException) as exc:
        await ServiceAuthenticator(identities=[_identity()]).authenticate_request(
            invalid, credentials=None, now=NOW
        )
    assert exc.value.detail["code"] == "AUTH_TIMESTAMP_INVALID"


def test_existing_bearer_authentication_remains_compatible(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_SYNC_SERVICE_TOKEN", "legacy-token")
    auth = ServiceAuthenticator(identities=[_identity()])
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="legacy-token"
    )

    principal = auth.authenticate(credentials)

    assert principal.authenticated is True
    assert principal.source_system == "service"
    assert principal.credential_id is None


def test_credential_identity_parses_server_side_json(monkeypatch):
    monkeypatch.setenv(
        "ENTERPRISE_SYNC_HMAC_CREDENTIALS",
        json.dumps(
            [
                {
                    "credentialId": "device-sync",
                    "keyId": "key-active",
                    "secret": SECRET,
                    "allowedBindings": [
                        {"tenantId": "tenant-a", "sourceSystem": "EAM"}
                    ],
                    "status": "active",
                    "validFrom": datetime.fromtimestamp(
                        NOW - 60, tz=timezone.utc
                    ).isoformat(),
                    "validUntil": datetime.fromtimestamp(
                        NOW + 60, tz=timezone.utc
                    ).isoformat(),
                }
            ]
        ),
    )
    auth = ServiceAuthenticator()

    identities = auth.identities

    assert len(identities) == 1
    assert identities[0].allowed_bindings == frozenset(
        {CredentialBinding("tenant-a", "EAM")}
    )
    assert "secret" not in repr(identities[0]).lower()


def test_production_default_replay_store_is_never_memory(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.delenv("ENTERPRISE_SERVICE_REPLAY_STORE", raising=False)
    monkeypatch.delenv("ENTERPRISE_REDIS_URL", raising=False)

    store = service_auth_module._default_replay_store()

    assert isinstance(store, RedisReplayStore)


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_gateway_db")
async def test_hmac_dependency_preserves_body_for_existing_endpoint(monkeypatch):
    from enterprise.gateway.app import app

    now = int(time.time())
    key_id = f"integration-{now}"
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "ENTERPRISE_SYNC_HMAC_CREDENTIALS",
        json.dumps(
            [
                {
                    "credentialId": "device-sync",
                    "keyId": key_id,
                    "secret": SECRET,
                    "allowedBindings": [
                        {"tenantId": "tenant-a", "sourceSystem": "EAM"}
                    ],
                    "status": "active",
                    "validFrom": now - 60,
                    "validUntil": now + 60,
                }
            ]
        ),
    )
    payload = {
        "eventId": f"evt-hmac-{now}",
        "sourceSystem": "EAM",
        "externalDocumentId": f"DOC-HMAC-{now}",
        "sourceVersionId": "v1",
        "sha256": hashlib.sha256(b"signed-content").hexdigest(),
        "fileName": "signed.pdf",
        "source": {"bucket": "test", "objectKey": "signed.pdf"},
        "metadata": {
            "schema_version": 1,
            "tenant_id": "tenant-a",
            "external_document_id": f"DOC-HMAC-{now}",
            "source_system": "EAM",
            "equipment_id": "EQ-1",
            "document_type": "manual",
            "document_version": "v1",
            "department_id": "dept-a",
            "security_level": 1,
            "business_status": "active",
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path = "/enterprise/api/v1/documents"
    signature = sign_request(
        secret=SECRET,
        timestamp=str(now),
        method="POST",
        path=path,
        body=body,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-TY-Timestamp": str(now),
                "X-TY-Key-Id": key_id,
                "X-TY-Signature": signature,
            },
        )

    assert response.status_code == 202
