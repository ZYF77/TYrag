"""P0 stabilization regression tests: error safety and contract calibration."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.app import app, error_response
from enterprise.gateway.sync.ragflow_document_client import sanitize_log_payload

ROOT = Path(__file__).resolve().parents[2]


def test_error_response_never_leaks_exception_text():
    resp = error_response("INTERNAL_ERROR", request_id="req-1")
    body = json.loads(resp.body.decode())
    assert "secret traceback" not in body
    assert body["code"] == "INTERNAL_ERROR"
    assert body["requestId"] == "req-1"


def test_sanitize_log_payload_redacts_and_truncates():
    raw = (
        '{"token":"top-secret-value","password":"pw-value","message":"ok"}'
        + "x" * 2000
    )
    sanitized = sanitize_log_payload(raw, limit=200)
    assert "top-secret-value" not in sanitized
    assert "pw-value" not in sanitized
    assert "<redacted>" in sanitized
    assert len(sanitized) <= 200


def test_sanitize_log_payload_redacts_camelcase_and_query_params():
    raw = (
        '{"accessToken":"camel-secret","refresh_token":"refresh-secret",'
        '"message":"ok"} token=query-secret apiKey=key-secret'
    )
    sanitized = sanitize_log_payload(raw)
    assert "camel-secret" not in sanitized
    assert "refresh-secret" not in sanitized
    assert "query-secret" not in sanitized
    assert "key-secret" not in sanitized
    assert '"accessToken": "<redacted>"' in sanitized
    assert '"refresh_token": "<redacted>"' in sanitized
    assert "token=<redacted>" in sanitized
    assert "apiKey=<redacted>" in sanitized


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_gateway_db")
class TestErrorResponseShape:
    async def test_validation_error_uses_error_response_shape(self):
        saved_enabled = os.environ.get("ENTERPRISE_SYNC_AUTH_ENABLED")
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "false"
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/enterprise/api/v1/documents", json={})
                assert resp.status_code == 422
                body = resp.json()
                assert body["code"] == "VALIDATION_ERROR"
                assert body["message"] == "请求内容不符合要求，请检查后重试。"
                assert body["requestId"]
                assert "detail" not in body
        finally:
            if saved_enabled is None:
                os.environ.pop("ENTERPRISE_SYNC_AUTH_ENABLED", None)
            else:
                os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = saved_enabled

    async def test_service_auth_error_uses_error_response_shape(self):
        saved_token = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN")
        saved_enabled = os.environ.get("ENTERPRISE_SYNC_AUTH_ENABLED")
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "test-token"
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "true"
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/enterprise/api/v1/documents", json={})
                assert resp.status_code == 401
                body = resp.json()
                assert body["code"] == "AUTH_TOKEN_MISSING"
                assert body["message"] == "Service token required"
                assert body["requestId"]
                assert "detail" not in body
        finally:
            if saved_token is None:
                os.environ.pop("ENTERPRISE_SYNC_SERVICE_TOKEN", None)
            else:
                os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = saved_token
            if saved_enabled is None:
                os.environ.pop("ENTERPRISE_SYNC_AUTH_ENABLED", None)
            else:
                os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = saved_enabled

    async def test_unknown_route_returns_registered_error_code(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/enterprise/api/v1/does-not-exist")
            assert resp.status_code == 404
            body = resp.json()
            assert body["code"] == "REQUEST_FAILED"
            assert body["requestId"]


class TestContractFiles:
    def test_openapi_marks_planned_endpoints(self):
        with open(ROOT / "contracts" / "integration-openapi.yaml", encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        paths = spec["paths"]
        assert paths["/documents"]["post"]["x-status"] == "implemented"
        assert paths["/documents/sync-status"]["get"]["x-status"] == "implemented"
        assert paths["/documents/{externalDocumentId}/disable"]["post"]["x-status"] == "implemented"
        assert paths["/documents/{externalDocumentId}/restore"]["post"]["x-status"] == "implemented"
        assert paths["/conversations"]["post"]["x-status"] == "implemented"
        assert paths["/conversations/{conversationId}"]["get"]["x-status"] == "implemented"
        assert paths["/conversations/{conversationId}/messages:stream"]["post"]["x-status"] == "implemented"
        assert paths["/citations/{citationId}"]["get"]["x-status"] == "implemented"

    def test_openapi_documents_status_has_real_query_parameters(self):
        with open(ROOT / "contracts" / "integration-openapi.yaml", encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        params = spec["paths"]["/documents/{externalDocumentId}/status"]["get"]["parameters"]
        inline_names = [p["name"] for p in params if "name" in p]
        component = spec["components"]["parameters"]["ExternalDocumentId"]
        assert component["name"] == "externalDocumentId"
        assert "tenant_id" in inline_names
        assert "refresh" in inline_names

    def test_error_codes_include_stabilized_codes(self):
        with open(ROOT / "contracts" / "error-codes.yaml", encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        codes = {item["code"] for item in spec["errors"]}
        assert {
            "AUTH_USER_DISABLED",
            "AUTH_TOKEN_MISSING",
            "DOCUMENT_NOT_FOUND",
            "VALIDATION_ERROR",
            "INTERNAL_ERROR",
            "REQUEST_FAILED",
        } <= codes

    def test_metadata_schema_is_valid_json(self):
        with open(ROOT / "contracts" / "metadata-schema.json", encoding="utf-8") as f:
            schema = json.load(f)
        assert schema["$schema"].startswith("https://json-schema.org/")

    def test_metadata_schema_allows_asset_id(self):
        with open(ROOT / "contracts" / "metadata-schema.json", encoding="utf-8") as f:
            schema = json.load(f)
        assert "asset_id" in schema["properties"]
