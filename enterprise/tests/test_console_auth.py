"""Local Console/Harness session authentication tests."""

from __future__ import annotations

import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.console_router import _failed_logins
from enterprise.gateway.auth.console_session import (
    FailedLoginLimiter,
    hash_console_password,
    verify_console_password,
)
from enterprise.gateway.app import app


@pytest.fixture
def console_env(monkeypatch):
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv("ENTERPRISE_CONSOLE_AUTH_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_CONSOLE_PASSWORD_HASH", hash_console_password(password))
    monkeypatch.setenv("ENTERPRISE_CONSOLE_SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("ENTERPRISE_CONSOLE_TENANT_ID", "eam-test-tenant")
    monkeypatch.setenv("ENTERPRISE_CONSOLE_COOKIE_SECURE", "false")
    monkeypatch.setenv("ENTERPRISE_CONSOLE_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.delenv("ENTERPRISE_SYNC_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ENTERPRISE_SYNC_HMAC_CREDENTIALS", raising=False)
    _failed_logins._failures.clear()
    _failed_logins._locked_until.clear()
    yield password
    _failed_logins._failures.clear()
    _failed_logins._locked_until.clear()


def test_console_password_hash_round_trip_without_plaintext_storage():
    password = secrets.token_urlsafe(24)
    encoded = hash_console_password(password)

    assert encoded.startswith("scrypt:v1:")
    assert verify_console_password(password, encoded)
    assert not verify_console_password(secrets.token_urlsafe(24), encoded)
    assert not verify_console_password(password, "not-a-password-hash")


def test_failed_login_limiter_locks_only_the_repeated_client_key():
    limiter = FailedLoginLimiter(max_attempts=2, window_seconds=10, lockout_seconds=5)

    limiter.record_failure("client-a", now=1)
    assert not limiter.is_blocked("client-a", now=2)
    limiter.record_failure("client-a", now=3)
    assert limiter.is_blocked("client-a", now=4)
    assert not limiter.is_blocked("client-b", now=4)
    assert not limiter.is_blocked("client-a", now=9)


@pytest.mark.asyncio
async def test_console_and_user_auth_share_one_cookie(console_env):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        before = await client.get("/enterprise/api/v1/console/auth/me")
        assert before.status_code == 401
        assert before.json()["code"] == "AUTH_TOKEN_MISSING"

        login = await client.post(
            "/enterprise/api/v1/console/auth/login",
            json={"username": "zkadmin", "password": console_env},
        )
        assert login.status_code == 200
        payload = login.json()
        assert payload["authenticated"] is True
        assert payload["username"] == "zkadmin"
        assert payload["tenantId"] == "eam-test-tenant"
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/enterprise/api" in cookie

        console_me = await client.get("/enterprise/api/v1/console/auth/me")
        assert console_me.status_code == 200
        assert console_me.json()["capabilities"]

        user_me = await client.get("/enterprise/api/v1/auth/me")
        assert user_me.status_code == 200
        assert user_me.json()["tenantId"] == "eam-test-tenant"
        assert "admin" in user_me.json()["capabilities"]

        # The runtime log is a user/capability route and accepts the same
        # local cookie without requiring a second login or Bearer token.
        runtime_log = await client.get("/enterprise/api/v1/diagnostics/http-log")
        assert runtime_log.status_code == 200

        logout = await client.post("/enterprise/api/v1/console/auth/logout")
        assert logout.status_code == 204
        after = await client.get("/enterprise/api/v1/console/auth/me")
        assert after.status_code == 401


@pytest.mark.asyncio
async def test_console_login_rejects_wrong_credentials_and_foreign_origin(console_env):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        wrong = await client.post(
            "/enterprise/api/v1/console/auth/login",
            json={"username": "zkadmin", "password": secrets.token_urlsafe(24)},
        )
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "CONSOLE_AUTH_INVALID"

        foreign = await client.post(
            "/enterprise/api/v1/console/auth/login",
            headers={"Origin": "https://untrusted.example"},
            json={"username": "zkadmin", "password": console_env},
        )
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "CONSOLE_CSRF_REJECTED"


@pytest.mark.asyncio
async def test_tampered_console_cookie_is_rejected(console_env):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        login = await client.post(
            "/enterprise/api/v1/console/auth/login",
            json={"username": "zkadmin", "password": console_env},
        )
        assert login.status_code == 200
        client.cookies.set("enterprise_console_session", "tampered", path="/enterprise/api")

        response = await client.get("/enterprise/api/v1/auth/me")
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_console_cookie_is_not_a_service_or_hmac_credential(console_env):
    import enterprise.gateway.app as app_module

    app_module.app.dependency_overrides[app_module.get_gateway_db] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/enterprise/api/v1/console/auth/login",
                json={"username": "zkadmin", "password": console_env},
            )
            assert login.status_code == 200
            response = await client.post(
                "/enterprise/api/v1/documents",
                json={
                    "eventId": "console-auth-test",
                    "sourceSystem": "EAM",
                    "externalDocumentId": "console-auth-test",
                    "sourceVersionId": "v1",
                    "sha256": "0" * 64,
                    "fileName": "console-auth-test.pdf",
                    "source": {"bucket": "test", "objectKey": "test"},
                    "metadata": {},
                },
            )
            assert response.status_code == 401
            assert response.json()["code"] == "AUTH_TOKEN_MISSING"
    finally:
        app_module.app.dependency_overrides.pop(app_module.get_gateway_db, None)
