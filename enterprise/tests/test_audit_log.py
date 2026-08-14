from __future__ import annotations

import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from enterprise.gateway.app import app
from enterprise.gateway.audit_log import (
    _redact,
    clear_http_events,
    list_http_events,
    write_feed_callback_audit,
    write_feed_register_audit,
    write_inquiry_audit,
)
from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.callback_delivery import (
    CallbackDeliveryWorker,
    CallbackEndpoint,
    enqueue_terminal_callback,
)
from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping


def test_redact_does_not_keep_secret_values():
    redacted = _redact(
        {
            "eventId": "evt-1",
            "secret": "should-not-appear",
            "nested": {"hmacSecret": "nope", "fileName": "a.pdf"},
        }
    )
    assert redacted["eventId"] == "evt-1"
    assert redacted["secret"] == "<redacted>"
    assert redacted["nested"]["hmacSecret"] == "<redacted>"
    assert redacted["nested"]["fileName"] == "a.pdf"


def test_audit_files_land_on_configured_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    write_feed_register_audit(
        method="POST",
        path="/enterprise/api/v3/documents",
        headers={"X-TY-Signature": "v1=abc", "Authorization": "Bearer secret-token"},
        body={"eventId": "evt-1", "fileName": "manual.pdf"},
        http_status=202,
    )
    write_feed_callback_audit(
        method="POST",
        url="http://192.168.30.31:5105/api/v1/ai/feed/callback",
        headers={"X-TY-Timestamp": "1786611092", "X-TY-Signature": "sha256=ab"},
        body={"status": "retrievable", "secret": "nope"},
        http_status=401,
        response_body='{"code":"AUTH_SIGNATURE_INVALID"}',
        delivery_id="d1",
        outcome="dead_letter",
    )
    register = json.loads((tmp_path / "feed-register.jsonl").read_text(encoding="utf-8"))
    callback = json.loads((tmp_path / "feed-callback.jsonl").read_text(encoding="utf-8"))
    assert register["kind"] == "feed.register.inbound"
    assert register["headers"]["X-TY-Signature"] == "v1=abc"
    assert register["headers"]["Authorization"] == "<redacted>"
    assert register["body"]["eventId"] == "evt-1"
    assert callback["kind"] == "feed.callback.outbound"
    assert callback["http_status"] == 401
    assert callback["body"]["secret"] == "<redacted>"
    assert "nope" not in (tmp_path / "feed-callback.jsonl").read_text(encoding="utf-8")
    write_inquiry_audit(
        method="POST",
        path="/enterprise/api/v2/conversations/c1/messages",
        query="",
        headers={"Authorization": "Bearer jwt-token"},
        body={"question": "q", "clientMessageId": "m1"},
        http_status=200,
        response_body={"status": "completed", "answer": "x" * 3000},
    )
    inquiry = json.loads((tmp_path / "inquiry.jsonl").read_text(encoding="utf-8"))
    assert inquiry["kind"] == "inquiry.http"
    assert inquiry["headers"]["Authorization"] == "<redacted>"
    assert inquiry["response_body"]["answer"].endswith("chars>")
    assert "jwt-token" not in (tmp_path / "inquiry.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_v3_register_writes_audit_log(isolated_gateway_db, monkeypatch, tmp_path):
    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    payload = {
        "eventId": "evt-audit-001",
        "eventType": "upsert",
        "tenantId": "tenant-a",
        "sourceSystem": "DEMO",
        "externalDocumentId": "DOC-AUDIT-001",
        "sourceVersionId": "v1",
        "sha256": hashlib.sha256(b"DOC-AUDIT-001").hexdigest(),
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "test-root",
            "relativePath": "manual.pdf",
            "size": 10,
        },
        "metadata": {
            "schema_version": 1,
            "tenant_id": "tenant-a",
            "external_document_id": "DOC-AUDIT-001",
            "source_system": "DEMO",
            "equipment_id": "EQ-001",
            "document_type": "PRODUCT_MANUAL",
            "document_version": "v1",
            "department_id": "maintenance",
            "security_level": 2,
            "business_status": "active",
            "allow_group_ids": ["maintenance"],
            "deny_group_ids": [],
        },
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/enterprise/api/v3/documents", json=payload)
            assert response.status_code == 202
    finally:
        app.dependency_overrides.pop(require_service_principal, None)
    lines = (tmp_path / "feed-register.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert lines
    record = json.loads(lines[-1])
    assert record["http_status"] == 202
    assert record["body"]["eventId"] == "evt-audit-001"


@pytest.mark.asyncio
async def test_callback_worker_writes_audit_log(isolated_gateway_db, monkeypatch, tmp_path):
    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(
        __import__("enterprise.gateway.config", fromlist=["config"]).config,
        "callback_enabled",
        True,
    )

    def handler(request: Request) -> Response:
        return Response(401, json={"code": "AUTH_SIGNATURE_INVALID"})

    endpoints = {
        "DEMO": CallbackEndpoint(url="https://eam.example/callback", secret="cb-secret")
    }
    db, _ = isolated_gateway_db
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-AUDIT-CB",
        source_version_id="v1",
        event_id="evt-audit-cb",
        sha256="a" * 64,
        file_name="manual.pdf",
        source_kind="FILE_SHARE",
    )
    doc = await insert_mapping(db, doc)
    await enqueue_terminal_callback(
        db,
        doc=doc,
        terminal_status="retrievable",
        quality_status="passed",
        retrievable=True,
        endpoints=endpoints,
        enabled=True,
        default_secret="cb-secret",
    )
    worker = CallbackDeliveryWorker(
        db,
        endpoints=endpoints,
        default_secret="cb-secret",
        http_client=__import__("httpx").AsyncClient(transport=MockTransport(handler)),
    )
    try:
        await worker.run_once()
    finally:
        await worker.close()
    record = json.loads((tmp_path / "feed-callback.jsonl").read_text(encoding="utf-8"))
    assert record["http_status"] == 401
    assert record["headers"]["X-TY-Signature"].startswith("sha256=")
    assert "cb-secret" not in (tmp_path / "feed-callback.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_inquiry_writes_audit_log(isolated_gateway_db, monkeypatch, tmp_path):
    from fastapi import FastAPI

    from enterprise.gateway.feed_audit_middleware import FeedRegisterAuditMiddleware
    from enterprise.gateway.query import v2_router

    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    db, _ = isolated_gateway_db
    application = FastAPI()
    application.add_middleware(FeedRegisterAuditMiddleware)
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_user_principal] = lambda: UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/enterprise/api/v2/conversations",
            json={"equipmentId": "EQ-001"},
            headers={"Authorization": "Bearer test-jwt"},
        )
        assert response.status_code == 201
    record = json.loads((tmp_path / "inquiry.jsonl").read_text(encoding="utf-8"))
    assert record["kind"] == "inquiry.http"
    assert record["http_status"] == 201
    assert record["path"] == "/enterprise/api/v2/conversations"
    headers = {key.lower(): value for key, value in record["headers"].items()}
    assert headers["authorization"] == "<redacted>"
    assert record["body"]["equipmentId"] == "EQ-001"
    assert "test-jwt" not in (tmp_path / "inquiry.jsonl").read_text(encoding="utf-8")


def _user() -> UserPrincipal:
    return UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions", "read"),
    )


@pytest.mark.asyncio
async def test_http_log_endpoint_requires_user_jwt(isolated_gateway_db):
    clear_http_events()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/enterprise/api/v1/diagnostics/http-log")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_MISSING"


@pytest.mark.asyncio
async def test_http_log_endpoint_lists_redacted_inbound(isolated_gateway_db):
    clear_http_events()
    app.dependency_overrides[require_user_principal] = _user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            me = await client.get(
                "/enterprise/api/v1/auth/me",
                headers={"Authorization": "Bearer secret-jwt"},
            )
            assert me.status_code == 200
            listed = await client.get(
                "/enterprise/api/v1/diagnostics/http-log",
                headers={"Authorization": "Bearer secret-jwt"},
            )
    finally:
        app.dependency_overrides.pop(require_user_principal, None)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert items
    assert items[0]["method"] == "GET"
    assert items[0]["path"] == "/enterprise/api/v1/auth/me"
    assert items[0]["direction"] == "inbound"
    assert items[0]["http_status"] == 200
    dumped = json.dumps(items)
    assert "secret-jwt" not in dumped
    assert not any(
        item.get("path") == "/enterprise/api/v1/diagnostics/http-log" for item in items
    )


@pytest.mark.asyncio
async def test_inquiry_also_lands_in_http_event_ring(isolated_gateway_db, monkeypatch, tmp_path):
    from fastapi import FastAPI

    from enterprise.gateway.feed_audit_middleware import FeedRegisterAuditMiddleware
    from enterprise.gateway.query import v2_router

    clear_http_events()
    monkeypatch.setenv("ENTERPRISE_AUDIT_LOG_DIR", str(tmp_path))
    db, _ = isolated_gateway_db
    application = FastAPI()
    application.add_middleware(FeedRegisterAuditMiddleware)
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_user_principal] = _user
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/enterprise/api/v2/conversations",
            json={"equipmentId": "EQ-001"},
            headers={"Authorization": "Bearer test-jwt"},
        )
        assert response.status_code == 201
    events = list_http_events()
    assert events
    assert events[0]["kind"] == "inquiry.http"
    assert events[0]["body"]["equipmentId"] == "EQ-001"
    assert "test-jwt" not in json.dumps(events)
