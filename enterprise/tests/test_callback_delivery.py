"""Unit and ASGI tests for FILE_SHARE terminal callback delivery."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from enterprise.gateway.app import app
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.callback import classify_delivery, sign_payload, verify_signature
from enterprise.gateway.callback_delivery import (
    CallbackDeliveryWorker,
    CallbackEndpoint,
    build_terminal_payload,
    enqueue_terminal_callback,
    get_callback_delivery,
    is_internal_callback_document,
    parse_callback_endpoints,
    resolve_callback_endpoint,
    retry_delay_seconds,
)
from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping


def test_callback_endpoints_resolve_source_and_tenant_binding():
    endpoints = parse_callback_endpoints(
        json.dumps(
            {
                "EAM": "https://eam.example/cb",
                "tenant-a|DEMO": {
                    "url": "https://demo.example/cb",
                    "secret": "demo-secret",
                    "keyId": "demo-key",
                },
            }
        ),
        default_secret="global-secret",
    )
    eam = resolve_callback_endpoint(
        endpoints, tenant_id="tenant-a", source_system="EAM"
    )
    demo = resolve_callback_endpoint(
        endpoints, tenant_id="tenant-a", source_system="DEMO"
    )
    assert eam is not None
    assert eam.url == "https://eam.example/cb"
    assert eam.secret == "global-secret"
    assert demo is not None
    assert demo.secret == "demo-secret"
    assert demo.key_id == "demo-key"


def test_terminal_callback_error_message_is_chinese():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="FAC-1244-ATT-39",
        source_version_id="v1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name="manual.pdf",
        source_kind="FILE_SHARE",
    )
    payload = build_terminal_payload(
        delivery_id="del-1",
        originating_event_id="evt-1",
        doc=doc,
        terminal_status="failed",
        quality_status=None,
        retrievable=False,
        error={
            "code": "INTERNAL_ERROR",
            "message": "Unexpected sync failure",
            "retryable": False,
        },
    )
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "服务开小差了，请稍后重试。"
    assert "Unexpected" not in payload["error"]["message"]

    source_missing = build_terminal_payload(
        delivery_id="del-2",
        originating_event_id="evt-2",
        doc=doc,
        terminal_status="failed",
        quality_status=None,
        retrievable=False,
        error={
            "code": "DOCUMENT_SOURCE_NOT_FOUND",
            "message": "FILE_SHARE source file was not found",
            "retryable": False,
        },
    )
    assert source_missing["error"]["message"] == "找不到源文件。"


def test_internal_fixture_ids_are_skipped():
    assert is_internal_callback_document("PROBE-GE22002-CERT-1")
    assert is_internal_callback_document("tyrag-e2e-1786517725268433017")
    assert not is_internal_callback_document("FAC-8252-ATT-24")
    assert not is_internal_callback_document("FAC-PROBE-REAL")


def test_retry_delay_follows_freeze_schedule():
    assert [retry_delay_seconds(i) for i in range(1, 9)] == [
        1,
        5,
        30,
        120,
        600,
        600,
        600,
        600,
    ]
    exhausted = classify_delivery(503, attempt=8, max_attempts=8)
    assert exhausted.status == "dead_letter"


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_terminal_status(
    isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_CALLBACK_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_CALLBACK_HMAC_SECRET", "cb-secret")
    monkeypatch.setenv(
        "ENTERPRISE_CALLBACK_ENDPOINTS",
        json.dumps({"DEMO": "https://eam.example/callback"}),
    )
    from enterprise.gateway import config as config_module

    monkeypatch.setattr(config_module.config, "callback_enabled", True)
    monkeypatch.setattr(config_module.config, "callback_hmac_secret", "cb-secret")

    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-001",
        source_version_id="v1",
        event_id="evt-cb-001",
        sha256="a" * 64,
        file_name="manual.pdf",
        source_kind="FILE_SHARE",
    )
    doc = await insert_mapping(db, doc)
    first = await enqueue_terminal_callback(
        db,
        doc=doc,
        terminal_status="review_required",
        quality_status="review_required",
        retrievable=False,
    )
    second = await enqueue_terminal_callback(
        db,
        doc=doc,
        terminal_status="review_required",
        quality_status="review_required",
        retrievable=False,
    )
    assert first is not None
    assert second is not None
    assert first.delivery_id == second.delivery_id
    stored = await get_callback_delivery(
        db,
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-001",
        source_version_id="v1",
        terminal_status="review_required",
    )
    assert stored is not None
    assert stored.state == "pending"


@pytest.mark.asyncio
async def test_enqueue_skips_internal_fixture_document_ids(
    isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_CALLBACK_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_CALLBACK_HMAC_SECRET", "cb-secret")
    monkeypatch.setenv(
        "ENTERPRISE_CALLBACK_ENDPOINTS",
        json.dumps({"EAM": "https://eam.example/callback"}),
    )
    from enterprise.gateway import config as config_module

    monkeypatch.setattr(config_module.config, "callback_enabled", True)
    monkeypatch.setattr(config_module.config, "callback_hmac_secret", "cb-secret")

    probe = ExtDocumentMap(
        tenant_id="wp04e2e",
        source_system="EAM",
        external_document_id="PROBE-GE22002-CERT-1",
        source_version_id="v-probe-1",
        event_id="evt-probe-1",
        sha256="c" * 64,
        file_name="probe.pdf",
        source_kind="FILE_SHARE",
    )
    probe = await insert_mapping(db, probe)
    skipped = await enqueue_terminal_callback(
        db,
        doc=probe,
        terminal_status="failed",
        quality_status="failed",
        retrievable=False,
        error={"code": "DOCUMENT_PARSE_FAILED", "message": "RAGFlow parsing failed"},
    )
    assert skipped is None
    assert await get_callback_delivery(
        db,
        tenant_id="wp04e2e",
        source_system="EAM",
        external_document_id="PROBE-GE22002-CERT-1",
        source_version_id="v-probe-1",
        terminal_status="failed",
    ) is None


@pytest.mark.asyncio
async def test_delivery_worker_posts_signed_payload_and_marks_delivered(
    isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setattr(
        __import__("enterprise.gateway.config", fromlist=["config"]).config,
        "callback_enabled",
        True,
    )
    seen: list[Request] = []

    def handler(request: Request) -> Response:
        seen.append(request)
        return Response(200, json={"ok": True})

    endpoints = {
        "DEMO": CallbackEndpoint(
            url="https://eam.example/callback",
            secret="cb-secret",
            key_id="outbound-1",
        )
    }
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-002",
        source_version_id="v1",
        event_id="evt-cb-002",
        sha256="b" * 64,
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
        count = await worker.run_once()
    finally:
        await worker.close()

    assert count == 1
    assert len(seen) == 1
    request = seen[0]
    body = request.content
    timestamp = int(request.headers["X-TY-Timestamp"])
    verify_signature(
        body,
        request.headers["X-TY-Signature"],
        "cb-secret",
        timestamp,
        now=timestamp,
    )
    assert request.headers["X-TY-Key-Id"] == "outbound-1"
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "retrievable"
    assert payload["eventType"] == "document.terminal"
    assert payload["payloadVersion"] == "1"
    assert payload["retrievable"] is True
    stored = await get_callback_delivery(
        db,
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-002",
        source_version_id="v1",
        terminal_status="retrievable",
    )
    assert stored is not None
    assert stored.state == "delivered"


@pytest.mark.asyncio
async def test_delivery_worker_retries_on_5xx(isolated_gateway_db, monkeypatch):
    db, _ = isolated_gateway_db
    monkeypatch.setattr(
        __import__("enterprise.gateway.config", fromlist=["config"]).config,
        "callback_enabled",
        True,
    )

    def handler(request: Request) -> Response:
        return Response(503, json={"error": "busy"})

    endpoints = {
        "DEMO": CallbackEndpoint(url="https://eam.example/callback", secret="cb-secret")
    }
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-003",
        source_version_id="v1",
        event_id="evt-cb-003",
        sha256="c" * 64,
        file_name="manual.pdf",
        source_kind="FILE_SHARE",
    )
    doc = await insert_mapping(db, doc)
    await enqueue_terminal_callback(
        db,
        doc=doc,
        terminal_status="failed",
        quality_status="failed",
        retrievable=False,
        error={"code": "DOCUMENT_QUALITY_FAILED", "message": "failed", "retryable": False},
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
    stored = await get_callback_delivery(
        db,
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-CB-003",
        source_version_id="v1",
        terminal_status="failed",
    )
    assert stored is not None
    assert stored.state == "pending"
    assert stored.attempts == 1
    assert stored.next_attempt_at is not None


@pytest.mark.asyncio
async def test_v3_registration_202_is_slim_accept_receipt(
    isolated_gateway_db, monkeypatch
):
    app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    payload = {
        "eventId": "evt-accept-001",
        "eventType": "upsert",
        "tenantId": "tenant-a",
        "sourceSystem": "DEMO",
        "externalDocumentId": "DOC-ACCEPT-001",
        "sourceVersionId": "v1",
        "sha256": hashlib.sha256(b"DOC-ACCEPT-001").hexdigest(),
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
            "external_document_id": "DOC-ACCEPT-001",
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
            response = await client.post(
                "/enterprise/api/v3/documents", json=payload
            )
            assert response.status_code == 202
            body = response.json()
            assert set(body) == {
                "operationId",
                "externalDocumentId",
                "sourceVersionId",
                "deduplicated",
                "updatedAt",
            }
            assert "statusUrl" not in body
            assert body["operationId"] == "evt-accept-001"
            status_path = (
                f"/enterprise/api/v3/documents/"
                f"{quote('DOC-ACCEPT-001', safe='')}/status"
                f"?tenantId=tenant-a&sourceSystem=DEMO&sourceVersionId=v1"
            )
            status = await client.get(status_path)
            assert status.status_code == 200
            assert "statusUrl" in status.json()
            assert status.json()["externalDocumentId"] == "DOC-ACCEPT-001"
    finally:
        app.dependency_overrides.pop(require_service_principal, None)


def test_build_terminal_payload_matches_freeze_fields():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="v1",
        event_id="evt-1",
        sha256="d" * 64,
        file_name="a.pdf",
    )
    payload = build_terminal_payload(
        delivery_id="del-1",
        originating_event_id="evt-1",
        doc=doc,
        terminal_status="failed",
        quality_status="failed",
        retrievable=False,
        error={"code": "X", "message": "y", "retryable": False},
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert payload["deliveryId"] == "del-1"
    assert payload["eventType"] == "document.terminal"
    assert payload["payloadVersion"] == "1"
    assert payload["status"] == "failed"
    signature = sign_payload(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
        "secret",
        100,
    )
    assert signature.startswith("sha256=")
