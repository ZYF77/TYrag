"""System-admin management APIs: integrations, EAM probe, metadata lists,
summary counters, conversation messages, parsed_at transitions.

Covers the internal endpoints under /enterprise/api/v1/admin/system:
authorization (system_admin vs normal users/auditors), callback binding
splitting and tenant visibility, the JWKS reachability probe (including the
"no POST to callbacks, no DB writes" guarantees), and tenant-isolated
conversation/document metadata listing with filtering, server-side ordering,
pagination, sensitive-field leakage checks, the summary endpoint, the
session-management message endpoint, and the parsed_at write-once-on-ready
state machine of update_mapping_status.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt
import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("ENTERPRISE_TEST_MODE", "1")
os.environ["JWT_SHARED_SECRET"] = "test-secret-must-be-at-least-32-bytes!!"

from enterprise.gateway import admin_router
from enterprise.gateway.app import app
from enterprise.gateway.config import config
from enterprise.gateway.db.dialect import exec_sql, fetchone
from enterprise.gateway.query.v2_store import create_conversation
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    get_mapping,
    insert_mapping,
    update_mapping_status,
)

BASE = "/enterprise/api/v1/admin/system"
SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"
JWT_ENV = {
    "JWT_ISSUER": "https://auth.example.com",
    "JWT_AUDIENCE": "tyrag-gateway",
    "JWT_ENABLE_HS": "true",
    "JWT_ALLOWED_ALGS": "HS256",
    "JWT_JWKS_URL": "",
}

_CONVERSATION_ITEM_KEYS = {
    "conversationId",
    "businessUserId",
    "equipmentId",
    "fixedAssetNo",
    "status",
    "ragflowChatId",
    "ragflowSessionId",
    "contextVersion",
    "createdAt",
    "lastMessageAt",
}

_DOCUMENT_ITEM_KEYS = {
    "externalDocumentId",
    "sourceVersionId",
    "currentVersion",
    "fileName",
    "sourceKind",
    "sourceSystem",
    "documentType",
    "equipmentId",
    "fixedAssetNo",
    "assetId",
    "syncStatus",
    "businessStatus",
    "ragflowDatasetId",
    "ragflowDocumentId",
    "sourceSize",
    "createdAt",
    "updatedAt",
    "parsedAt",
    "eamNotifiedAt",
}


def _make_token(claims: dict | None = None, secret: str = SHARED_SECRET) -> str:
    payload = {
        "sub": "admin-user-001",
        "tenant": "customer-a",
        "roles": ["system_admin"],
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + 3600,
        "iss": "https://auth.example.com",
        "aud": "tyrag-gateway",
    }
    if claims:
        payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_conversation(
    gateway,
    *,
    conversation_id: str,
    tenant_id: str = "customer-a",
    business_user_id: str = "admin-user-001",
    status: str = "active",
    equipment_id: str | None = None,
    fixed_asset_no: str | None = None,
    last_message_at: str | None = None,
    created_at: str | None = None,
    title: str | None = None,
    context_summary: str | None = None,
    message_body: str | None = None,
) -> None:
    async with gateway.transaction(write=True) as conn:
        await create_conversation(
            conn,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            business_user_id=business_user_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
            fault_code=None,
        )
        updates: list[str] = []
        params: list[object] = []
        if status != "active":
            updates.append("status=?")
            params.append(status)
        if last_message_at is not None:
            updates.append("last_message_at=?")
            params.append(last_message_at)
        if created_at is not None:
            updates.append("created_at=?")
            params.append(created_at)
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if context_summary is not None:
            updates.append("context_summary=?")
            params.append(context_summary)
        if updates:
            params.extend([conversation_id, tenant_id, business_user_id])
            await exec_sql(
                conn,
                f"""UPDATE ext_v2_conversation SET {', '.join(updates)}
                    WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
                tuple(params),
            )
        if message_body is not None:
            await exec_sql(
                conn,
                """INSERT INTO ext_v2_message
                   (message_id, conversation_id, tenant_id, business_user_id,
                    role, content, status, created_at)
                   VALUES (?, ?, ?, ?, 'user', ?, 'completed', ?)""",
                (
                    f"MSG-{uuid.uuid4().hex}",
                    conversation_id,
                    tenant_id,
                    business_user_id,
                    message_body,
                    last_message_at or "2026-08-12T10:00:00+00:00",
                ),
            )


async def _seed_message(
    gateway,
    *,
    conversation_id: str,
    role: str,
    content: str,
    created_at: str,
    tenant_id: str = "customer-a",
    business_user_id: str = "admin-user-001",
    status: str = "completed",
) -> None:
    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """INSERT INTO ext_v2_message
               (message_id, conversation_id, tenant_id, business_user_id,
                role, content, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"MSG-{uuid.uuid4().hex}",
                conversation_id,
                tenant_id,
                business_user_id,
                role,
                content,
                status,
                created_at,
            ),
        )


async def _seed_diagnostic_run(
    gateway,
    *,
    run_id: str,
    conversation_id: str,
    tenant_id: str = "customer-a",
    business_user_id: str = "admin-user-001",
    with_diagnostics: bool = True,
) -> None:
    result = {"answer": "must-not-be-returned-by-admin-diagnostics"}
    if with_diagnostics:
        result["_diagnostics"] = {
            "version": 1,
            "runId": run_id,
            "startedAt": "2026-08-31T08:00:00.000Z",
            "durationMs": 12.5,
            "truncated": False,
            "events": [
                {"type": "scope", "atMs": 1, "data": {"actualDocumentIds": ["doc-1"]}},
                {"type": "outcome", "atMs": 12, "data": {"outcome": "completed"}},
            ],
        }
    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """INSERT INTO ext_v2_message_run
               (conversation_id, tenant_id, business_user_id, client_message_id,
                request_hash, run_id, status, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
            (
                conversation_id,
                tenant_id,
                business_user_id,
                f"client-{run_id}",
                f"hash-{run_id}",
                run_id,
                json.dumps(result),
                "2026-08-31T08:00:00.000Z",
            ),
        )


async def _seed_document(
    gateway,
    *,
    external_id: str,
    tenant_id: str = "customer-a",
    source_system: str = "EAM",
    sync_status: str = "ready",
    file_name: str | None = None,
    updated_at: str | None = None,
    **extra,
) -> None:
    payload = dict(
        tenant_id=tenant_id,
        source_system=source_system,
        external_document_id=external_id,
        source_version_id="v1",
        event_id=f"EV-{uuid.uuid4().hex}",
        sha256=hashlib.sha256(external_id.encode()).hexdigest(),
        file_name=file_name or f"{external_id}.pdf",
        sync_status=sync_status,
    )
    payload.update(extra)
    async with gateway.transaction(write=True) as conn:
        doc = await insert_mapping(conn, ExtDocumentMap(**payload))
        if updated_at is not None and doc.id is not None:
            await exec_sql(
                conn,
                "UPDATE ext_document_map SET updated_at=? WHERE id=?",
                (updated_at, doc.id),
            )


async def _seed_callback_delivery(
    gateway,
    *,
    external_id: str,
    state: str = "delivered",
    updated_at: str,
    processing_round: int = 1,
    tenant_id: str = "customer-a",
    source_system: str = "EAM",
) -> None:
    """Insert one callback_delivery row for the EAM-notified-at timestamp."""
    suffix = uuid.uuid4().hex
    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """INSERT INTO callback_delivery
               (delivery_id, originating_event_id, tenant_id, source_system,
                external_document_id, source_version_id, processing_round,
                terminal_status, payload_json, payload_hash, endpoint_url,
                state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', '{}', ?, ?, ?, ?, ?)""",
            (
                f"DLC-{suffix}",
                f"EV-{suffix}",
                tenant_id,
                source_system,
                external_id,
                "v1",
                processing_round,
                f"hash-{suffix}",
                "https://eam.example.com/callback",
                state,
                updated_at,
                updated_at,
            ),
        )


async def _count_callback_tables(gateway) -> int:
    """Total row count of callback-related tables (delivery + outbox)."""
    async with gateway.transaction(write=False) as conn:
        delivery = await fetchone(conn, "SELECT COUNT(*) AS n FROM callback_delivery")
        outbox = await fetchone(conn, "SELECT COUNT(*) AS n FROM sync_outbox")
    return int(delivery["n"]) + int(outbox["n"])


@pytest.fixture
def jwt_env(monkeypatch):
    for key, value in JWT_ENV.items():
        monkeypatch.setenv(key, value)


# ---------- Authorization ----------


@pytest.mark.asyncio
class TestAdminAuthorization:
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_system_admin_can_access_all_four_endpoints(self, jwt_env):
        token = _make_token()
        paths = [
            "/integrations",
            "/metadata/conversations",
            "/metadata/documents",
            "/diagnostics/traces",
        ]
        async with _client() as client:
            for path in paths:
                resp = await client.get(f"{BASE}{path}", headers=_auth(token))
                assert resp.status_code == 200, resp.text
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "nope"}
            )
            # Authorization passes; unknown binding is a 404, not a 403.
            assert resp.status_code == 404
            assert resp.json()["code"] == "PROBE_TARGET_NOT_FOUND"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_end_user_forbidden_on_all_endpoints(self, jwt_env):
        token = _make_token({"sub": "end-user-001", "roles": ["end_user"]})
        async with _client() as client:
            for path in (
                "/integrations",
                "/metadata/conversations",
                "/metadata/documents",
                "/diagnostics/traces",
            ):
                resp = await client.get(f"{BASE}{path}", headers=_auth(token))
                assert resp.status_code == 403
                assert resp.json()["code"] == "ACL_DENIED"
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "EAM"}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_auditor_forbidden_on_all_endpoints(self, jwt_env):
        token = _make_token({"sub": "audit-user-001", "roles": ["auditor"]})
        async with _client() as client:
            for path in (
                "/integrations",
                "/metadata/conversations",
                "/metadata/documents",
                "/diagnostics/traces",
            ):
                resp = await client.get(f"{BASE}{path}", headers=_auth(token))
                assert resp.status_code == 403
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "EAM"}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_missing_token_unauthorized(self, jwt_env):
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_admin_routes_not_in_openapi_schema(self, jwt_env):
        schema = app.openapi()
        for path in (
            f"{BASE}/integrations",
            f"{BASE}/eam-probe",
            f"{BASE}/metadata/conversations",
            f"{BASE}/metadata/documents",
            f"{BASE}/diagnostics/traces",
            f"{BASE}/diagnostics/traces/{{run_id}}",
        ):
            assert path not in schema["paths"]


# ---------- Integrations ----------


@pytest.mark.asyncio
class TestRagDiagnostics:
    async def test_list_and_detail_are_tenant_isolated(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_diagnostic_run(
            gateway, run_id="run-visible", conversation_id="conv-visible"
        )
        await _seed_diagnostic_run(
            gateway,
            run_id="run-other-tenant",
            conversation_id="conv-other",
            tenant_id="customer-b",
        )
        await _seed_diagnostic_run(
            gateway,
            run_id="run-without-diagnostics",
            conversation_id="conv-no-diagnostics",
            with_diagnostics=False,
        )
        token = _make_token()
        async with _client() as client:
            listed = await client.get(
                f"{BASE}/diagnostics/traces", headers=_auth(token)
            )
            detail = await client.get(
                f"{BASE}/diagnostics/traces/run-visible", headers=_auth(token)
            )
            cross_tenant = await client.get(
                f"{BASE}/diagnostics/traces/run-other-tenant", headers=_auth(token)
            )
            absent = await client.get(
                f"{BASE}/diagnostics/traces/run-without-diagnostics",
                headers=_auth(token),
            )

        assert listed.status_code == detail.status_code == 200
        assert [item["runId"] for item in listed.json()["items"]] == ["run-visible"]
        assert "must-not-be-returned" not in listed.text
        assert detail.json()["diagnostics"]["runId"] == "run-visible"
        assert detail.json()["diagnostics"]["events"][0]["type"] == "scope"
        assert cross_tenant.status_code == absent.status_code == 404

    async def test_detail_requires_admin(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_diagnostic_run(
            gateway, run_id="run-admin-only", conversation_id="conv-admin-only"
        )
        token = _make_token({"sub": "end-user-001", "roles": ["end_user"]})
        async with _client() as client:
            response = await client.get(
                f"{BASE}/diagnostics/traces/run-admin-only", headers=_auth(token)
            )

        assert response.status_code == 403
        assert response.json()["code"] == "ACL_DENIED"


@pytest.mark.asyncio
class TestIntegrations:
    ENDPOINTS = json.dumps(
        {
            "EAM": "https://eam.example.com/base/callback?token=x",
            "customer-a|MES": {
                "url": "https://mes-a.example.com/hook",
                "secret": "SUPER-SECRET-VALUE",
            },
            "customer-a|SCADA": {
                "url": "https://scada-a.example.com/hook",
                "keyId": "key-1",
            },
            "customer-b|MES": {"url": "https://mes-b.example.com/hook"},
        }
    )

    async def test_binding_split_and_tenant_visibility(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        monkeypatch.setattr(config, "callback_hmac_secret", "", raising=False)
        monkeypatch.setattr(config, "callback_enabled", True, raising=False)
        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENDPOINTS", self.ENDPOINTS)
        token = _make_token()
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        bindings = {item["binding"] for item in data["callbacks"]}
        # Global binding + own-tenant bindings only; customer-b is invisible.
        assert bindings == {"EAM", "customer-a|MES", "customer-a|SCADA"}
        # The global flag must be surfaced and match per-item "enabled".
        assert data["callbacksEnabled"] is True

        eam = next(i for i in data["callbacks"] if i["binding"] == "EAM")
        assert eam == {
            "binding": "EAM",
            "tenantId": None,
            "sourceSystem": "EAM",
            "baseUrl": "https://eam.example.com",
            "path": "/base/callback?token=x",
            "method": "POST",
            "enabled": True,
            "credentialConfigured": False,
        }
        mes = next(i for i in data["callbacks"] if i["binding"] == "customer-a|MES")
        assert mes["tenantId"] == "customer-a"
        assert mes["sourceSystem"] == "MES"
        assert mes["baseUrl"] == "https://mes-a.example.com"
        assert mes["path"] == "/hook"
        assert mes["method"] == "POST"
        scada = next(i for i in data["callbacks"] if i["binding"] == "customer-a|SCADA")
        assert scada["credentialConfigured"] is True

        # No credential material anywhere in the response.
        assert "SUPER-SECRET-VALUE" not in resp.text
        assert "secret" not in resp.text.lower().replace("credentialconfigured", "")

    async def test_ragflow_card_shape(self, isolated_gateway_db, jwt_env, monkeypatch):
        monkeypatch.setattr(config, "callback_enabled", False, raising=False)
        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENDPOINTS", "")
        token = _make_token()
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["callbacksEnabled"] is False
        assert data["ragflow"]["baseUrl"] == config.ragflow_base_url
        assert data["ragflow"]["apiVersion"] == config.ragflow_api_version
        prefix = f"/api/{config.ragflow_api_version}"
        assert data["ragflow"]["paths"] == {
            "health": f"{prefix}/system/ping",
            "datasets": f"{prefix}/datasets",
            "chats": f"{prefix}/chats",
            "completions": f"{prefix}/chat/completions",
            "retrieval": f"{prefix}/retrieval",
        }

    async def test_disabled_callback_flag_and_empty_config(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        monkeypatch.setattr(config, "callback_enabled", False, raising=False)
        monkeypatch.delenv("ENTERPRISE_CALLBACK_ENDPOINTS", raising=False)
        token = _make_token()
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["callbacksEnabled"] is False
        assert data["callbacks"] == []

        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENDPOINTS", self.ENDPOINTS)
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["callbacksEnabled"] is False
        for item in resp.json()["callbacks"]:
            assert item["enabled"] is False

    async def test_malformed_endpoints_env_returns_empty(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENDPOINTS", "not-valid-json{")
        token = _make_token()
        async with _client() as client:
            resp = await client.get(f"{BASE}/integrations", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["callbacks"] == []


# ---------- EAM probe ----------


def _mock_client_factory(handler) -> "callable":
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=httpx.Timeout(5.0),
        )

    return factory


@pytest.mark.asyncio
class TestEamProbe:
    def _setup_endpoints(self, monkeypatch) -> None:
        monkeypatch.setattr(
            config, "callback_hmac_secret", "PROBE-HMAC-SECRET-VALUE", raising=False
        )
        monkeypatch.setenv(
            "ENTERPRISE_CALLBACK_ENDPOINTS",
            json.dumps({"EAM": "https://eam.example.com/device/callback"}),
        )

    async def test_success_is_get_jwks_and_writes_nothing(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        self._setup_endpoints(monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # Proves the probe never POSTs and never hits the callback path.
            assert request.method == "GET"
            assert request.url.path == "/.well-known/jwks.json"
            assert str(request.url).startswith("https://eam.example.com")
            return httpx.Response(200, json={"keys": [{"kty": "RSA", "kid": "k1"}]})

        monkeypatch.setattr(
            admin_router, "_build_probe_client", _mock_client_factory(handler)
        )
        gateway, _ = isolated_gateway_db
        before = await _count_callback_tables(gateway)
        token = _make_token()
        async with _client() as client:
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "EAM"}
            )
        after = await _count_callback_tables(gateway)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["binding"] == "EAM"
        assert data["probeUrl"] == "https://eam.example.com/.well-known/jwks.json"
        assert data["status"] == "connected"
        assert data["httpStatus"] == 200
        assert isinstance(data["latencyMs"], int) and data["latencyMs"] >= 0
        assert data["checkedAt"]
        assert "errorCode" not in data
        # No response body, no HMAC secret in the output.
        assert "PROBE-HMAC-SECRET-VALUE" not in resp.text
        assert "kty" not in resp.text
        # Probe must not create callback deliveries or outbox events.
        assert before == after
        assert len(seen) == 1

    @pytest.mark.parametrize(
        ("case", "http_status", "expected_code"),
        [
            ("timeout", None, "PROBE_TIMEOUT"),
            ("connect_error", None, "PROBE_CONNECT_FAILED"),
            ("http_500", 500, "PROBE_HTTP_ERROR"),
            ("http_401", 401, "PROBE_HTTP_ERROR"),
            ("redirect", 302, "PROBE_REDIRECTED"),
            ("invalid_json", 200, "PROBE_INVALID_RESPONSE"),
            ("missing_keys", 200, "PROBE_INVALID_RESPONSE"),
            ("keys_not_list", 200, "PROBE_INVALID_RESPONSE"),
        ],
    )
    async def test_failure_branches(
        self, isolated_gateway_db, jwt_env, monkeypatch, case, http_status, expected_code
    ):
        self._setup_endpoints(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/.well-known/jwks.json"
            if case == "timeout":
                raise httpx.ConnectTimeout("probe timed out", request=request)
            if case == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if case == "http_500":
                return httpx.Response(500, text="boom")
            if case == "http_401":
                return httpx.Response(401, text="denied")
            if case == "redirect":
                return httpx.Response(
                    302, headers={"location": "https://elsewhere.example.com/x"}
                )
            if case == "invalid_json":
                return httpx.Response(200, text="this is not json")
            if case == "missing_keys":
                return httpx.Response(200, json={"iss": "https://eam.example.com"})
            if case == "keys_not_list":
                return httpx.Response(200, json={"keys": "abc"})
            raise AssertionError(f"unknown case {case}")

        monkeypatch.setattr(
            admin_router, "_build_probe_client", _mock_client_factory(handler)
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "EAM"}
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "failed"
        assert data["errorCode"] == expected_code
        if http_status is None:
            assert data["httpStatus"] is None
        else:
            assert data["httpStatus"] == http_status
        assert data["binding"] == "EAM"

    async def test_unknown_binding_returns_404(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        self._setup_endpoints(monkeypatch)
        token = _make_token()
        async with _client() as client:
            resp = await client.post(
                f"{BASE}/eam-probe",
                headers=_auth(token),
                json={"binding": "UNKNOWN-SYSTEM"},
            )
        assert resp.status_code == 404
        data = resp.json()
        assert data["code"] == "PROBE_TARGET_NOT_FOUND"
        assert data["message"] == "未找到该回调配置。"

    async def test_other_tenant_binding_not_probeable(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        monkeypatch.setattr(config, "callback_hmac_secret", "", raising=False)
        monkeypatch.setenv(
            "ENTERPRISE_CALLBACK_ENDPOINTS",
            json.dumps({"customer-b|EAM": "https://eam-b.example.com/callback"}),
        )
        token = _make_token()  # tenant customer-a
        async with _client() as client:
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "customer-b|EAM"}
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "PROBE_TARGET_NOT_FOUND"

    async def test_probe_failure_branch_writes_nothing(
        self, isolated_gateway_db, jwt_env, monkeypatch
    ):
        self._setup_endpoints(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(503, text="unavailable")

        monkeypatch.setattr(
            admin_router, "_build_probe_client", _mock_client_factory(handler)
        )
        gateway, _ = isolated_gateway_db
        before = await _count_callback_tables(gateway)
        token = _make_token()
        async with _client() as client:
            resp = await client.post(
                f"{BASE}/eam-probe", headers=_auth(token), json={"binding": "EAM"}
            )
        after = await _count_callback_tables(gateway)
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert before == after


# ---------- Conversation metadata ----------


@pytest.mark.asyncio
class TestConversationMetadata:
    async def test_tenant_isolation_and_ordering(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-a-old",
            last_message_at="2026-08-10T09:00:00+00:00",
            equipment_id="EQ-100",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-a-new",
            last_message_at="2026-08-12T09:00:00+00:00",
            fixed_asset_no="FA-200",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-b-hidden",
            tenant_id="customer-b",
            last_message_at="2026-08-13T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations", headers=_auth(token)
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [item["conversationId"] for item in data["items"]] == [
            "conv-a-new",
            "conv-a-old",
        ]
        assert data["hasMore"] is False
        first = data["items"][0]
        assert first["fixedAssetNo"] == "FA-200"
        assert data["items"][1]["equipmentId"] == "EQ-100"
        assert set(first.keys()) == _CONVERSATION_ITEM_KEYS

    async def test_status_filter(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-active",
            status="active",
            last_message_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-archived",
            status="archived",
            last_message_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            archived = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"status": "archived"},
            )
            active = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"status": "active"},
            )
        assert archived.status_code == 200
        assert [i["conversationId"] for i in archived.json()["items"]] == ["conv-archived"]
        assert active.status_code == 200
        assert [i["conversationId"] for i in active.json()["items"]] == ["conv-active"]

    async def test_pagination_and_has_more(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        for index in range(3):
            await _seed_conversation(
                gateway,
                conversation_id=f"conv-page-{index}",
                last_message_at=f"2026-08-1{index}T09:00:00+00:00",
            )
        token = _make_token()
        async with _client() as client:
            page1 = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"limit": 2, "offset": 0},
            )
            page2 = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"limit": 2, "offset": 2},
            )
        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["hasMore"] is True
        assert body1["items"][0]["conversationId"] == "conv-page-2"
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body2["items"][0]["conversationId"] == "conv-page-0"
        assert body2["hasMore"] is False

    async def test_sensitive_fields_not_leaked(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-secret",
            last_message_at="2026-08-12T09:00:00+00:00",
            title="SECRET-TITLE-X",
            context_summary="SECRET-SUMMARY-X",
            message_body="SECRET-MESSAGE-BODY",
            equipment_id="EQ-SECRET",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations", headers=_auth(token)
            )
        assert resp.status_code == 200
        text = resp.text
        assert "SECRET-TITLE-X" not in text
        assert "SECRET-SUMMARY-X" not in text
        assert "SECRET-MESSAGE-BODY" not in text
        assert "title" not in text.lower()
        assert "summary" not in text.lower()
        items = resp.json()["items"]
        assert [i["conversationId"] for i in items] == ["conv-secret"]

    async def test_order_by_created_at_asc_and_desc(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-ord-old",
            created_at="2026-08-01T09:00:00+00:00",
            last_message_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-ord-new",
            created_at="2026-08-05T09:00:00+00:00",
            last_message_at="2026-08-11T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            asc = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"orderBy": "createdAt", "order": "asc"},
            )
            desc = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"orderBy": "createdAt", "order": "desc"},
            )
        assert asc.status_code == 200
        assert [i["conversationId"] for i in asc.json()["items"]] == [
            "conv-ord-old",
            "conv-ord-new",
        ]
        assert desc.status_code == 200
        assert [i["conversationId"] for i in desc.json()["items"]] == [
            "conv-ord-new",
            "conv-ord-old",
        ]

    async def test_illegal_order_by_and_order_fall_back_to_default(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-fb-old",
            last_message_at="2026-08-10T09:00:00+00:00",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-fb-new",
            last_message_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            for params in (
                {"orderBy": "evil; DROP TABLE ext_v2_conversation", "order": "asc"},
                {"orderBy": "createdAt", "order": "sideways"},
                {"orderBy": "createdAt", "order": "ASC"},
            ):
                resp = await client.get(
                    f"{BASE}/metadata/conversations",
                    headers=_auth(token),
                    params=params,
                )
            assert resp.status_code == 200, resp.text
            # 大写 ASC 被接受（order 大小写不敏感），走 createdAt 升序。
            assert [i["conversationId"] for i in resp.json()["items"]] == [
                "conv-fb-old",
                "conv-fb-new",
            ]
            illegal = await client.get(
                f"{BASE}/metadata/conversations",
                headers=_auth(token),
                params={"orderBy": "evil; DROP TABLE ext_v2_conversation"},
            )
        # 非法 orderBy 回退默认 lastMessageAt DESC，不报错。
        assert illegal.status_code == 200, illegal.text
        assert [i["conversationId"] for i in illegal.json()["items"]] == [
            "conv-fb-new",
            "conv-fb-old",
        ]


# ---------- Document metadata ----------


@pytest.mark.asyncio
class TestDocumentMetadata:
    async def test_tenant_isolation_and_updated_at_ordering(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-A-OLD",
            updated_at="2026-08-10T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-A-NEW",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-B-HIDDEN",
            tenant_id="customer-b",
            updated_at="2026-08-13T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents", headers=_auth(token)
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [i["externalDocumentId"] for i in data["items"]] == [
            "DOC-A-NEW",
            "DOC-A-OLD",
        ]
        assert data["hasMore"] is False
        first = data["items"][0]
        assert first["fileName"] == "DOC-A-NEW.pdf"
        assert first["sourceSystem"] == "EAM"
        assert first["syncStatus"] == "ready"
        assert set(first.keys()) == _DOCUMENT_ITEM_KEYS

    async def test_status_and_source_system_filter(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-READY",
            source_system="EAM",
            sync_status="ready",
            updated_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-FAILED",
            source_system="MES",
            sync_status="failed",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            failed = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"status": "failed"},
            )
            mes = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"sourceSystem": "MES"},
            )
            none = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"sourceSystem": "MES", "status": "ready"},
            )
        assert failed.status_code == 200
        assert [i["externalDocumentId"] for i in failed.json()["items"]] == ["DOC-FAILED"]
        assert mes.status_code == 200
        assert [i["externalDocumentId"] for i in mes.json()["items"]] == ["DOC-FAILED"]
        assert none.status_code == 200
        assert none.json()["items"] == []

    async def test_pagination_and_has_more(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        for index in range(3):
            await _seed_document(
                gateway,
                external_id=f"DOC-PAGE-{index}",
                updated_at=f"2026-08-1{index}T09:00:00+00:00",
            )
        token = _make_token()
        async with _client() as client:
            page1 = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"limit": 2, "offset": 0},
            )
            page2 = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"limit": 2, "offset": 2},
            )
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["hasMore"] is True
        assert body1["items"][0]["externalDocumentId"] == "DOC-PAGE-2"
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body2["items"][0]["externalDocumentId"] == "DOC-PAGE-0"
        assert body2["hasMore"] is False

    async def test_sensitive_fields_not_leaked(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        sha = hashlib.sha256(b"DOC-SECRET").hexdigest()
        await _seed_document(
            gateway,
            external_id="DOC-SECRET",
            sha256=sha,
            bucket="SECRET-BUCKET",
            object_key="SECRET-OBJECT-KEY",
            relative_path="SECRET-STORAGE-PATH",
            allow_group_ids='["SECRET-ACL-GROUP"]',
            last_error_message="SECRET-ERROR-STACK",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents", headers=_auth(token)
            )
        assert resp.status_code == 200
        text = resp.text
        assert sha not in text
        assert "SECRET-BUCKET" not in text
        assert "SECRET-OBJECT-KEY" not in text
        assert "SECRET-STORAGE-PATH" not in text
        assert "SECRET-ACL-GROUP" not in text
        assert "SECRET-ERROR-STACK" not in text
        assert "objectKey" not in text
        assert "bucket" not in text.lower()
        assert "sha256" not in text.lower()
        items = resp.json()["items"]
        assert [i["externalDocumentId"] for i in items] == ["DOC-SECRET"]

    async def test_order_by_file_name(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-ORD-B",
            file_name="b-report.pdf",
            updated_at="2026-08-10T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-ORD-A",
            file_name="a-report.pdf",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            asc = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"orderBy": "fileName", "order": "asc"},
            )
            desc = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"orderBy": "fileName", "order": "desc"},
            )
        assert asc.status_code == 200, asc.text
        assert [i["fileName"] for i in asc.json()["items"]] == [
            "a-report.pdf",
            "b-report.pdf",
        ]
        assert desc.status_code == 200
        assert [i["fileName"] for i in desc.json()["items"]] == [
            "b-report.pdf",
            "a-report.pdf",
        ]

    async def test_illegal_ordering_falls_back_to_updated_at_desc(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-FB-OLD",
            updated_at="2026-08-10T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-FB-NEW",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"orderBy": "1=1; DROP TABLE ext_document_map", "order": "ASC"},
            )
        assert resp.status_code == 200, resp.text
        assert [i["externalDocumentId"] for i in resp.json()["items"]] == [
            "DOC-FB-OLD",
            "DOC-FB-NEW",
        ]

    async def test_business_status_filter(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-BS-ACTIVE",
            business_status="active",
            updated_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-BS-REVIEW",
            business_status="review_required",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            review = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"businessStatus": "review_required"},
            )
            active = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"business_status": "active"},
            )
        assert review.status_code == 200
        assert [i["externalDocumentId"] for i in review.json()["items"]] == [
            "DOC-BS-REVIEW"
        ]
        assert [i["businessStatus"] for i in review.json()["items"]] == [
            "review_required"
        ]
        # snake_case 别名与 sourceSystem 的处理方式一致。
        assert active.status_code == 200
        assert [i["externalDocumentId"] for i in active.json()["items"]] == [
            "DOC-BS-ACTIVE"
        ]

    async def test_parsed_at_and_eam_notified_at_fields(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-EAM-1",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-EAM-0",
            updated_at="2026-08-13T09:00:00+00:00",
        )
        # 两条 delivered 回调取 MAX(updated_at)；dead_letter 不计入。
        await _seed_callback_delivery(
            gateway,
            external_id="DOC-EAM-1",
            state="delivered",
            updated_at="2026-08-12T12:00:00+00:00",
            processing_round=1,
        )
        await _seed_callback_delivery(
            gateway,
            external_id="DOC-EAM-1",
            state="delivered",
            updated_at="2026-08-12T15:00:00+00:00",
            processing_round=2,
        )
        await _seed_callback_delivery(
            gateway,
            external_id="DOC-EAM-1",
            state="dead_letter",
            updated_at="2026-08-12T18:00:00+00:00",
            processing_round=3,
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents", headers=_auth(token)
            )
            by_eam = await client.get(
                f"{BASE}/metadata/documents",
                headers=_auth(token),
                params={"orderBy": "eamNotifiedAt", "order": "desc"},
            )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 2
        first = next(
            i for i in items if i["externalDocumentId"] == "DOC-EAM-1"
        )
        assert set(first.keys()) == _DOCUMENT_ITEM_KEYS
        assert first["parsedAt"] is None  # 未经过 update_mapping_status，不回填
        assert first["eamNotifiedAt"] == "2026-08-12T15:00:00+00:00"
        # 默认 updatedAt DESC：无回调的 DOC-EAM-0 更新时间更晚，排最前。
        assert items[0]["externalDocumentId"] == "DOC-EAM-0"
        # orderBy=eamNotifiedAt DESC：别名排序生效。PostgreSQL 默认把 NULL
        # 当作最大值（DESC 时 NULLS FIRST），因此无送达时间的 DOC-EAM-0 排最前。
        assert by_eam.status_code == 200, by_eam.text
        by_eam_ids = [i["externalDocumentId"] for i in by_eam.json()["items"]]
        assert by_eam_ids == ["DOC-EAM-0", "DOC-EAM-1"]

    async def test_eam_notified_at_empty_without_delivered_callback(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_document(
            gateway,
            external_id="DOC-EAM-2",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_callback_delivery(
            gateway,
            external_id="DOC-EAM-2",
            state="pending",
            updated_at="2026-08-12T12:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents", headers=_auth(token)
            )
        assert resp.status_code == 200
        assert resp.json()["items"][0]["eamNotifiedAt"] is None


# ---------- Metadata summary ----------


@pytest.mark.asyncio
class TestMetadataSummary:
    async def test_totals_and_group_counts(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-sum-1",
            status="active",
            last_message_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-sum-2",
            status="archived",
            last_message_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-SUM-READY",
            sync_status="ready",
            business_status="active",
            updated_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-SUM-FAILED",
            sync_status="failed",
            business_status="review_required",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(f"{BASE}/metadata/summary", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["conversations"] == {
            "total": 2,
            "byStatus": {"active": 1, "archived": 1},
        }
        assert data["documents"]["total"] == 2
        assert data["documents"]["bySyncStatus"] == {"ready": 1, "failed": 1}
        assert data["documents"]["byBusinessStatus"] == {
            "active": 1,
            "review_required": 1,
        }

    async def test_tenant_isolation(self, isolated_gateway_db, jwt_env):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-sum-own",
            last_message_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_conversation(
            gateway,
            conversation_id="conv-sum-other",
            tenant_id="customer-b",
            last_message_at="2026-08-12T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-SUM-OWN",
            updated_at="2026-08-11T09:00:00+00:00",
        )
        await _seed_document(
            gateway,
            external_id="DOC-SUM-OTHER",
            tenant_id="customer-b",
            updated_at="2026-08-12T09:00:00+00:00",
        )
        token = _make_token()  # tenant customer-a
        async with _client() as client:
            resp = await client.get(f"{BASE}/metadata/summary", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversations"]["total"] == 1
        assert data["documents"]["total"] == 1

    async def test_end_user_forbidden(self, isolated_gateway_db, jwt_env):
        token = _make_token({"sub": "end-user-001", "roles": ["end_user"]})
        async with _client() as client:
            resp = await client.get(f"{BASE}/metadata/summary", headers=_auth(token))
        assert resp.status_code == 403
        assert resp.json()["code"] == "ACL_DENIED"

    async def test_summary_not_in_openapi_schema(self, jwt_env):
        schema = app.openapi()
        for path in (
            f"{BASE}/metadata/summary",
            f"{BASE}/metadata/conversations/x/messages",
        ):
            assert path not in schema["paths"]


# ---------- Conversation messages (session management) ----------


@pytest.mark.asyncio
class TestConversationMessages:
    async def test_returns_user_and_assistant_in_created_at_order(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-msg-1",
            last_message_at="2026-08-12T10:05:00+00:00",
        )
        await _seed_message(
            gateway,
            conversation_id="conv-msg-1",
            role="user",
            content="ADMIN-QUESTION-TEXT",
            created_at="2026-08-12T10:00:00+00:00",
        )
        await _seed_message(
            gateway,
            conversation_id="conv-msg-1",
            role="assistant",
            content="ADMIN-ANSWER-TEXT",
            status="completed",
            created_at="2026-08-12T10:05:00+00:00",
        )
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations/conv-msg-1/messages",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["conversationId"] == "conv-msg-1"
        assert len(data["items"]) == 2
        first, second = data["items"]
        assert set(first.keys()) == {
            "messageId",
            "role",
            "content",
            "status",
            "createdAt",
        }
        assert first["role"] == "user"
        assert first["content"] == "ADMIN-QUESTION-TEXT"
        assert first["status"] == "completed"
        assert first["createdAt"] == "2026-08-12T10:00:00+00:00"
        assert second["role"] == "assistant"
        assert second["content"] == "ADMIN-ANSWER-TEXT"
        assert first["messageId"] != second["messageId"]

    async def test_message_bodies_absent_from_metadata_list_endpoints(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-msg-2",
            last_message_at="2026-08-12T10:00:00+00:00",
            message_body="SECRET-MESSAGE-BODY-META",
        )
        token = _make_token()
        async with _client() as client:
            listed = await client.get(
                f"{BASE}/metadata/conversations", headers=_auth(token)
            )
            messages = await client.get(
                f"{BASE}/metadata/conversations/conv-msg-2/messages",
                headers=_auth(token),
            )
        # 元数据列表端点绝不回显消息正文。
        assert listed.status_code == 200
        assert "SECRET-MESSAGE-BODY-META" not in listed.text
        # 会话管理端点按管理员要求回显正文，但不含 citations/reasoning/附件。
        assert messages.status_code == 200
        assert "SECRET-MESSAGE-BODY-META" in messages.text
        body = messages.text.lower()
        assert "citations" not in body
        assert "reasoning" not in body
        assert "attachments" not in body
        assert "businessuserid" not in body
        assert "business_user_id" not in body

    async def test_cross_tenant_conversation_is_404(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        await _seed_conversation(
            gateway,
            conversation_id="conv-msg-hidden",
            tenant_id="customer-b",
            last_message_at="2026-08-12T10:00:00+00:00",
        )
        await _seed_message(
            gateway,
            conversation_id="conv-msg-hidden",
            tenant_id="customer-b",
            role="user",
            content="OTHER-TENANT-QUESTION",
            created_at="2026-08-12T10:00:00+00:00",
        )
        token = _make_token()  # tenant customer-a
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations/conv-msg-hidden/messages",
                headers=_auth(token),
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"
        assert "OTHER-TENANT-QUESTION" not in resp.text

    async def test_unknown_conversation_is_404(self, isolated_gateway_db, jwt_env):
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations/conv-does-not-exist/messages",
                headers=_auth(token),
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_end_user_forbidden(self, isolated_gateway_db, jwt_env):
        token = _make_token({"sub": "end-user-001", "roles": ["end_user"]})
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/conversations/conv-msg-1/messages",
                headers=_auth(token),
            )
        assert resp.status_code == 403

    async def test_blank_conversation_id_is_422(self, isolated_gateway_db, jwt_env):
        token = _make_token()
        async with _client() as client:
            # %20 到达处理器后 strip 为空串 → 422 VALIDATION_ERROR。
            resp = await client.get(
                f"{BASE}/metadata/conversations/%20/messages",
                headers=_auth(token),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"


# ---------- parsed_at write-once-on-ready transitions ----------


@pytest.mark.asyncio
class TestDocumentParsedAtTransitions:
    async def _insert_document(self, conn, external_id: str) -> ExtDocumentMap:
        return await insert_mapping(
            conn,
            ExtDocumentMap(
                tenant_id="customer-a",
                source_system="EAM",
                external_document_id=external_id,
                source_version_id="v1",
                event_id=f"EV-{uuid.uuid4().hex}",
                sha256=hashlib.sha256(external_id.encode()).hexdigest(),
                file_name=f"{external_id}.pdf",
                sync_status="received",
            ),
        )

    async def _persisted_parsed_at(self, gateway, external_id: str):
        async with gateway.transaction(write=False) as conn:
            doc = await get_mapping(conn, "customer-a", "EAM", external_id, "v1")
        assert doc is not None
        return doc.parsed_at

    async def test_non_ready_to_ready_writes_parsed_at(
        self, isolated_gateway_db, jwt_env
    ):
        gateway, _ = isolated_gateway_db
        async with gateway.transaction(write=True) as conn:
            doc = await self._insert_document(conn, "DOC-PA-FIRST")
            assert doc.parsed_at is None
            assert await update_mapping_status(conn, doc, "ready") is True
            assert doc.parsed_at is not None
            first = doc.parsed_at
        assert await self._persisted_parsed_at(gateway, "DOC-PA-FIRST") == first
        # 列表端点可见该时间戳。
        token = _make_token()
        async with _client() as client:
            resp = await client.get(
                f"{BASE}/metadata/documents", headers=_auth(token)
            )
        items = resp.json()["items"]
        target = next(
            i for i in items if i["externalDocumentId"] == "DOC-PA-FIRST"
        )
        assert target["parsedAt"] == first

    async def test_already_ready_does_not_overwrite(self, isolated_gateway_db):
        gateway, _ = isolated_gateway_db
        async with gateway.transaction(write=True) as conn:
            doc = await self._insert_document(conn, "DOC-PA-KEEP")
            assert await update_mapping_status(conn, doc, "ready") is True
            first = doc.parsed_at
        time.sleep(0.02)  # 若被覆盖，utc_now() 必然产生不同时间戳
        async with gateway.transaction(write=True) as conn:
            assert (
                await update_mapping_status(conn, doc, "ready", pipeline_status="DONE")
                is True
            )
            assert doc.parsed_at == first
        assert await self._persisted_parsed_at(gateway, "DOC-PA-KEEP") == first

    async def test_failed_then_ready_refreshes_parsed_at(self, isolated_gateway_db):
        gateway, _ = isolated_gateway_db
        async with gateway.transaction(write=True) as conn:
            doc = await self._insert_document(conn, "DOC-PA-RETRY")
            assert await update_mapping_status(conn, doc, "ready") is True
            first = doc.parsed_at
        time.sleep(0.02)
        # ready→failed 不得清空 parsed_at。
        async with gateway.transaction(write=True) as conn:
            assert (
                await update_mapping_status(
                    conn,
                    doc,
                    "failed",
                    error_code="DOCUMENT_PARSE_FAILED",
                    error_message="解析失败",
                    last_error_retryable=True,
                )
                is True
            )
            assert doc.parsed_at == first
        time.sleep(0.02)
        # failed→ready（重试轮次成功）刷新 parsed_at。
        async with gateway.transaction(write=True) as conn:
            assert await update_mapping_status(conn, doc, "ready") is True
            assert doc.parsed_at is not None
            assert doc.parsed_at != first
        assert (
            await self._persisted_parsed_at(gateway, "DOC-PA-RETRY") == doc.parsed_at
        )

    async def test_non_ready_transitions_leave_parsed_at_untouched(
        self, isolated_gateway_db
    ):
        gateway, _ = isolated_gateway_db
        async with gateway.transaction(write=True) as conn:
            doc = await self._insert_document(conn, "DOC-PA-PARSING")
            assert (
                await update_mapping_status(
                    conn, doc, "parsing", pipeline_status="RUNNING"
                )
                is True
            )
            assert doc.parsed_at is None
            assert await update_mapping_status(conn, doc, "ready") is True
            first = doc.parsed_at
        time.sleep(0.02)
        async with gateway.transaction(write=True) as conn:
            assert (
                await update_mapping_status(
                    conn, doc, "validated", event_status="completed"
                )
                is True
            )
            assert doc.parsed_at == first
        assert await self._persisted_parsed_at(gateway, "DOC-PA-PARSING") == first
