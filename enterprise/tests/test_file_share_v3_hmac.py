"""FILE_SHARE v3 request-level HMAC integration tests."""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import quote, urlsplit

import enterprise.gateway.auth.service_auth as service_auth_module
import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.service_auth import (
    CredentialBinding,
    CredentialIdentity,
    MemoryReplayStore,
    ServiceAuthenticator,
    sign_request,
)
from enterprise.gateway.app import app


TEST_SECRET = "v3-hmac-integration-secret"


def _payload() -> dict:
    document_id = "HMAC-V3-DOC-001"
    tenant_id = "tenant-a"
    source_system = "EAM"
    version_id = "v1"
    return {
        "eventId": "evt-hmac-v3-001",
        "eventType": "upsert",
        "tenantId": tenant_id,
        "sourceSystem": source_system,
        "externalDocumentId": document_id,
        "sourceVersionId": version_id,
        "sha256": hashlib.sha256(b"fixture-pdf").hexdigest(),
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "test-root",
            "relativePath": "manual.pdf",
            "size": 11,
            "etag": "fixture-v1",
        },
        "metadata": {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "external_document_id": document_id,
            "source_system": source_system,
            "equipment_id": "EQ-001",
            "fixed_asset_no": "FA-001",
            "document_type": "PRODUCT_MANUAL",
            "document_version": version_id,
            "department_id": "maintenance",
            "security_level": 2,
            "business_status": "active",
            "allow_group_ids": ["maintenance"],
            "deny_group_ids": [],
            "page_count": 1,
        },
    }


def _headers(*, method: str, path: str, query: str = "", body: bytes = b"") -> dict[str, str]:
    timestamp = str(int(time.time()))
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-TY-Timestamp": timestamp,
        "X-TY-Key-Id": "v3-test-key",
        "X-TY-Signature": sign_request(
            secret=TEST_SECRET,
            timestamp=timestamp,
            method=method,
            path=path,
            query=query,
            body=body,
        ),
    }


@pytest.mark.asyncio
async def test_v3_registration_and_server_status_url_use_real_hmac_dependency(
    isolated_gateway_db, monkeypatch
):
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_SERVICE_REPLAY_STORE", "memory")
    monkeypatch.setenv("ENTERPRISE_EAM_ASSET_RESOLVER_MODE", "http")
    monkeypatch.setenv(
        "ENTERPRISE_EAM_ASSET_RESOLVER_BASE_URL", "http://127.0.0.1:1"
    )
    monkeypatch.setenv(
        "ENTERPRISE_EAM_ASSET_RESOLVER_PATH",
        "/api/integration/v1/assets/resolve",
    )
    monkeypatch.delenv("ENTERPRISE_EAM_ASSET_RESOLVER_TOKEN", raising=False)
    monkeypatch.setattr(
        service_auth_module,
        "_service_auth",
        ServiceAuthenticator(
            identities=[
                CredentialIdentity(
                    credential_id="v3-test-credential",
                    key_id="v3-test-key",
                    secret=TEST_SECRET,
                    allowed_bindings=frozenset(
                        {CredentialBinding("tenant-a", "EAM")}
                    ),
                )
            ],
            replay_store=MemoryReplayStore(),
        ),
    )

    payload = _payload()
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    registration_path = "/enterprise/api/v3/documents"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            registration_path,
            content=body,
            headers=_headers(method="POST", path=registration_path, body=body),
        )

        assert response.status_code == 202
        receipt = response.json()
        assert "statusUrl" not in receipt
        assert receipt["operationId"] == payload["eventId"]
        assert receipt["externalDocumentId"] == payload["externalDocumentId"]
        status_url = (
            f"/enterprise/api/v3/documents/"
            f"{quote(payload['externalDocumentId'], safe='')}/status"
            f"?tenantId={quote(payload['tenantId'], safe='')}"
            f"&sourceSystem={quote(payload['sourceSystem'], safe='')}"
            f"&sourceVersionId={quote(payload['sourceVersionId'], safe='')}"
        )
        parsed = urlsplit(status_url)
        status_response = await client.get(
            status_url,
            headers=_headers(
                method="GET", path=parsed.path, query=parsed.query
            ),
        )

        invalid_headers = _headers(
            method="POST", path=registration_path, body=body
        )
        invalid_headers["X-TY-Signature"] = "v1=" + ("0" * 64)
        invalid = await client.post(
            registration_path, content=body, headers=invalid_headers
        )

    assert parsed.scheme == ""
    assert parsed.netloc == ""
    assert parsed.path.endswith("/status")
    assert status_response.status_code == 200
    assert status_response.json()["externalDocumentId"] == "HMAC-V3-DOC-001"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "AUTH_SIGNATURE_INVALID"
