"""M1-E producer tests using only an in-process fake HTTP opener."""

import json
from urllib.parse import urlsplit

from enterprise.gateway.auth.service_auth import sign_request
from enterprise.scripts.m1e_document_producer import DocumentProducer, ProducerConfig


TEST_SECRET = "m1e-unit-test-value"


class FakeResponse:
    def __init__(self, payload: dict, status: int = 202):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class RecordingOpener:
    def __init__(self, payload: dict):
        self.payload = payload
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload)


def _producer() -> DocumentProducer:
    return DocumentProducer(
        ProducerConfig(
            base_url="http://gateway.test/enterprise/api/v2",
            key_id="m1e-key",
            secret=TEST_SECRET,
            tenant_id="demo-tenant",
            source_system="equipment-system",
        )
    )


def _headers(request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def test_submit_document_signs_raw_post_body_without_bearer_or_secret():
    payload = {
        "eventId": "m1e-event-001",
        "eventType": "upsert",
        "tenantId": "demo-tenant",
        "sourceSystem": "equipment-system",
        "externalDocumentId": "M1E-DOC-001",
    }
    opener = RecordingOpener({"operationId": "op-001", "status": "received"})

    result = _producer().submit_document(payload, timestamp="1700000000", opener=opener)

    assert result["operationId"] == "op-001"
    assert opener.timeout == 15.0
    request = opener.request
    assert request is not None
    assert request.get_method() == "POST"
    assert request.full_url == "http://gateway.test/enterprise/api/v2/documents"
    headers = _headers(request)
    assert "authorization" not in headers
    assert headers["x-ty-key-id"] == "m1e-key"
    assert headers["x-ty-timestamp"] == "1700000000"
    assert headers["x-ty-signature"] == sign_request(
        secret=TEST_SECRET,
        timestamp="1700000000",
        method="POST",
        path="/enterprise/api/v2/documents",
        body=request.data,
    )
    assert TEST_SECRET.encode("utf-8") not in request.data
    assert TEST_SECRET not in request.full_url
    assert TEST_SECRET not in repr(_producer().config)


def test_get_document_status_signs_canonical_query_and_uses_no_bearer():
    opener = RecordingOpener({"externalDocumentId": "M1E-DOC-001", "status": "ready"})

    result = _producer().get_document_status(
        "M1E-DOC-001",
        source_version_id="v1",
        timestamp="1700000001",
        opener=opener,
    )

    assert result["status"] == "ready"
    request = opener.request
    assert request is not None
    assert request.get_method() == "GET"
    parsed = urlsplit(request.full_url)
    assert parsed.path == "/enterprise/api/v2/documents/M1E-DOC-001/status"
    assert "tenantId=demo-tenant" in parsed.query
    assert "sourceSystem=equipment-system" in parsed.query
    assert "sourceVersionId=v1" in parsed.query
    headers = _headers(request)
    assert "authorization" not in headers
    assert headers["x-ty-signature"] == sign_request(
        secret=TEST_SECRET,
        timestamp="1700000001",
        method="GET",
        path=parsed.path,
        query=parsed.query,
        body=b"",
    )
    assert TEST_SECRET not in request.full_url
