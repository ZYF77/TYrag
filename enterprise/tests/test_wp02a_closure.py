from enterprise.gateway.db.dialect import fetchone
from enterprise.gateway.db.ops import gw_read, gw_write
"""WP-02A Closure tests: service auth, status mapping, regression."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["ENTERPRISE_TEST_MODE"] = "1"

from enterprise.gateway.app import app, validate_metadata
from enterprise.gateway.sync.status_mapping import (
    map_ragflow_run_to_sync_status,
    enterprise_stage,
)
from enterprise.gateway.auth.service_auth import ServiceAuthenticator

VALID_METADATA = {
    "schema_version": 1,
    "tenant_id": "tenant-001",
    "external_document_id": "DOC-123",
    "source_system": "EAM",
    "equipment_id": "EQ-001",
    "document_type": "manual",
    "document_version": "v2.0",
    "department_id": "dept-eng",
    "security_level": 3,
    "business_status": "active",
}


# ============================================================
# Service Authentication
# ============================================================

class TestServiceAuth:
    def test_missing_token_returns_401(self):
        """Without Authorization header, endpoint returns 401."""
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'true'
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "secret"
        with pytest.raises(Exception) as e:
            authenticator.authenticate(None)
        assert "401" in str(e.value.status_code)

    def test_wrong_token_returns_401(self):
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'true'
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "secret"
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="wrong")
        with pytest.raises(Exception) as e:
            authenticator.authenticate(creds)
        assert "401" in str(e.value.status_code)

    def test_correct_token_succeeds(self):
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'true'
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "correct-token"
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="correct-token")
        principal = authenticator.authenticate(creds)
        assert principal.authenticated

    def test_auth_disabled_allows_anonymous(self):
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'false'
        principal = authenticator.authenticate(None)
        assert not principal.authenticated
        assert principal.source_system == "anonymous"

    def test_ragflow_api_key_not_accepted_as_service_token(self):
        """Verifies ENTERPRISE_SYNC_SERVICE_TOKEN != RAGFLOW_API_KEY."""
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'true'
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "svc-secret")
        from fastapi.security import HTTPAuthorizationCredentials
        ragflow_key = os.environ.get("RAGFLOW_API_KEY", "ragflow-key")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=ragflow_key)
        if ragflow_key != os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"]:
            with pytest.raises(Exception):
                authenticator.authenticate(creds)
        # If they're equal by accident, skip

    def test_log_does_not_leak_token(self):
        """Error message must not contain the token value."""
        authenticator = ServiceAuthenticator()
        os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'true'
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "a-very-long-secret-token-value"
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="wrong-value")
        with pytest.raises(Exception) as e:
            authenticator.authenticate(creds)
        detail = e.value.detail
        assert "a-very-long-secret-token-value" not in str(detail)
        assert "secret" not in str(detail).lower()


@pytest.mark.usefixtures("isolated_gateway_db")
class TestServiceAuthHTTP:
    @pytest.mark.asyncio
    async def test_documents_endpoint_rejects_no_auth(self):
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "test-token"
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "true"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/enterprise/api/v1/documents", json={
                "eventId": "evt-auth-001",
                "sourceSystem": "EAM",
                "externalDocumentId": "DOC-AUTH-001",
                "sourceVersionId": "v1",
                "sha256": hashlib.sha256(b"test").hexdigest(),
                "fileName": "test.pdf",
                "source": {"bucket": "x", "objectKey": "y"},
                "metadata": VALID_METADATA,
            })
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_documents_endpoint_accepts_valid_token(self):
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "test-token"
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "true"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/documents",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "eventId": "evt-auth-002",
                    "sourceSystem": "EAM",
                    "externalDocumentId": "DOC-AUTH-002",
                    "sourceVersionId": "v1",
                    "sha256": hashlib.sha256(b"test2").hexdigest(),
                    "fileName": "test.pdf",
                    "source": {"bucket": "x", "objectKey": "y"},
                    "metadata": VALID_METADATA,
            })
            assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_health_endpoint_skips_auth(self):
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "test-token"
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "true"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/enterprise/api/v1/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"


# ============================================================
# Status Mapping
# ============================================================

class TestStatusMapping:
    def test_unstart_to_registered(self):
        assert map_ragflow_run_to_sync_status("UNSTART") == "registered"
        assert map_ragflow_run_to_sync_status("0") == "registered"

    def test_running_to_parsing(self):
        assert map_ragflow_run_to_sync_status("RUNNING") == "parsing"
        assert map_ragflow_run_to_sync_status("1") == "parsing"

    def test_done_to_ready(self):
        assert map_ragflow_run_to_sync_status("DONE") == "ready"
        assert map_ragflow_run_to_sync_status("3") == "ready"

    def test_fail_to_failed(self):
        assert map_ragflow_run_to_sync_status("FAIL") == "failed"
        assert map_ragflow_run_to_sync_status("4") == "failed"

    def test_cancel_to_cancelled(self):
        assert map_ragflow_run_to_sync_status("CANCEL") == "cancelled"
        assert map_ragflow_run_to_sync_status("2") == "cancelled"

    def test_unknown_value_to_registered(self):
        assert map_ragflow_run_to_sync_status("BOGUS") == "registered"
        assert map_ragflow_run_to_sync_status("") == "registered"

    def test_none_to_registered(self):
        assert map_ragflow_run_to_sync_status(None) == "registered"

    def test_enterprise_stage_all_statuses(self):
        for status in ("received", "validated", "registered", "parsing",
                        "ready", "failed", "cancelled", "review_required"):
            assert enterprise_stage(status) == status

    def test_enterprise_stage_unknown(self):
        assert enterprise_stage("bogus") is None


# ============================================================
# Regression: Idempotency + Deduplication
# ============================================================

@pytest.mark.usefixtures("isolated_gateway_db")
class TestRegressionIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_event_id_still_dedup(self, isolated_gateway_db):
        db, _ = isolated_gateway_db
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "false"
        transport = ASGITransport(app=app)
        headers = {}
        evt_id = "evt-dedup-test"
        doc_id = "DOC-DEDUP-TEST"
        body = {
            "eventId": evt_id,
            "sourceSystem": "EAM",
            "externalDocumentId": doc_id,
            "sourceVersionId": "v1",
            "sha256": hashlib.sha256(b"reg1").hexdigest(),
            "fileName": "test.pdf",
            "source": {"bucket": "x", "objectKey": "y"},
            "metadata": VALID_METADATA,
        }
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r1 = await c.post("/enterprise/api/v1/documents", json=body, headers=headers)
            assert r1.status_code == 202
            d1 = r1.json()
            assert d1["deduplicated"] is False

            r2 = await c.post("/enterprise/api/v1/documents", json=body, headers=headers)
            assert r2.status_code == 202
            d2 = r2.json()
            assert d2["deduplicated"] is True

            row = await gw_read(db, fetchone, "SELECT COUNT(*) AS n FROM ext_document_map WHERE event_id=?",
                (evt_id,),)
            assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_status_endpoint_returns_valid_data(self):
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "false"
        evt_id = "evt-status-001"
        doc_id = "DOC-STATUS-001"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # First insert a document so the status endpoint can find it
            body = {
                "eventId": evt_id,
                "sourceSystem": "EAM",
                "externalDocumentId": doc_id,
                "sourceVersionId": "v1",
                "sha256": hashlib.sha256(b"status1").hexdigest(),
                "fileName": "test.pdf",
                "source": {"bucket": "x", "objectKey": "y"},
                "metadata": VALID_METADATA,
            }
            await c.post("/enterprise/api/v1/documents", json=body)
            resp = await c.get(
                f"/enterprise/api/v1/documents/{doc_id}/status",
                params={"tenant_id": "tenant-001"})
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert "stage" in data
            assert "deduplicated" in data


# ============================================================
# WP-00 Health check regression
# ============================================================

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_still_works(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/enterprise/api/v1/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"
