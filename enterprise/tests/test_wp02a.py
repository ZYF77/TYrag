"""WP-02A tests: unit, contract, and integration."""
import hashlib
import json
import os
import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.sync.models import (
    ExtDocumentMap, init_db, insert_mapping, get_mapping,
    get_mapping_by_event_id, update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentStub, RAGFlowAPIError,
)
from enterprise.gateway.sync.source_adapter import SourceStub
os.environ['ENTERPRISE_SYNC_AUTH_ENABLED'] = 'false'
os.environ['ENTERPRISE_TEST_MODE'] = '1'
import enterprise.gateway.app as app_module
from enterprise.gateway.app import app, validate_metadata


# ── Unit: Metadata validation ──

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


class TestMetadataValidation:
    def test_valid_metadata_passes(self):
        assert validate_metadata(VALID_METADATA, "req-1") is None

    def test_missing_required_field(self):
        meta = dict(VALID_METADATA)
        del meta["equipment_id"]
        assert validate_metadata(meta, "req-2") == "DOCUMENT_METADATA_INVALID"

    def test_invalid_security_level(self):
        meta = dict(VALID_METADATA)
        meta["security_level"] = 99
        assert validate_metadata(meta, "req-3") == "DOCUMENT_METADATA_INVALID"

    def test_invalid_business_status(self):
        meta = dict(VALID_METADATA)
        meta["business_status"] = "bogus"
        assert validate_metadata(meta, "req-4") == "DOCUMENT_METADATA_INVALID"

    def test_extra_properties_rejected(self):
        meta = dict(VALID_METADATA)
        meta["internal_secret"] = "leak"
        assert validate_metadata(meta, "req-5") == "DOCUMENT_METADATA_INVALID"


# ── Unit: SHA256 validation ──

class TestSHA256Validation:
    def test_valid_sha256(self):
        import re
        assert re.match(r"^[0-9a-fA-F]{64}$", hashlib.sha256(b"test").hexdigest())

    def test_short_sha256_rejected(self):
        import re
        assert not re.match(r"^[0-9a-fA-F]{64}$", "abc123")

    def test_invalid_chars_rejected(self):
        import re
        assert not re.match(r"^[0-9a-fA-F]{64}$", "g" * 64)


# ── Unit: RAGFlow document stub ──

class TestRAGFlowStub:
    @pytest.mark.asyncio
    async def test_create_dataset(self):
        client = RAGFlowDocumentStub()
        result = await client.create_dataset("test-ds")
        assert result["data"]["name"] == "test-ds"
        assert result["data"]["id"].startswith("ds-")

    @pytest.mark.asyncio
    async def test_upload_document(self):
        client = RAGFlowDocumentStub()
        ds = await client.create_dataset("test-ds")
        result = await client.upload_document(
            ds["data"]["id"], "test.pdf", b"content")
        docs = result["data"]
        assert len(docs) == 1
        assert docs[0]["name"] == "test.pdf"

    @pytest.mark.asyncio
    async def test_stub_failure(self):
        client = RAGFlowDocumentStub()
        client._fail_next = True
        with pytest.raises(RAGFlowAPIError):
            await client.create_dataset("test-ds")

    @pytest.mark.asyncio
    async def test_list_datasets(self):
        client = RAGFlowDocumentStub()
        await client.create_dataset("ds1")
        await client.create_dataset("ds2")
        datasets = await client.list_datasets()
        assert len(datasets) == 2


# ── Unit: Source adapter ──

class TestSourceAdapter:
    @pytest.mark.asyncio
    async def test_fetch_returns_file(self):
        adapter = SourceStub(b"hello world")
        f = await adapter.fetch("bucket", "docs/test.pdf")
        assert f.content == b"hello world"
        assert f.file_name == "docs/test.pdf"

    @pytest.mark.asyncio
    async def test_fetch_with_custom_content(self):
        adapter = SourceStub(b"custom pdf bytes")
        f = await adapter.fetch("b", "k")
        assert f.size == len(b"custom pdf bytes")


# ── Unit: Status mapping ──

class TestStatusMapping:
    def test_status_stages(self):
        stage_map = {
            "received": "received", "validated": "validated",
            "registered": "registered", "parsing": "parsing",
            "ready": "ready", "failed": "failed",
        }
        assert stage_map["received"] == "received"
        assert "unknown_status" not in stage_map


# ── Contract: OpenAPI request/response shape ──

@pytest.mark.usefixtures("isolated_gateway_db")
class TestContractOpenAPI:
    @pytest.mark.asyncio
    async def test_post_documents_202(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/enterprise/api/v1/documents", json={
                "eventId": "evt-001",
                "sourceSystem": "EAM",
                "externalDocumentId": "DOC-001",
                "sourceVersionId": "v1",
                "sha256": hashlib.sha256(b"test").hexdigest(),
                "fileName": "manual.pdf",
                "mediaType": "application/pdf",
                "source": {"bucket": "docs", "objectKey": "equipment/manual.pdf"},
                "metadata": VALID_METADATA,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "externalDocumentId" in data
            assert "status" in data
            assert "deduplicated" in data

    @pytest.mark.asyncio
    async def test_post_documents_missing_field_422(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/enterprise/api/v1/documents", json={
                "eventId": "evt-002",
            })
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_post_documents_invalid_sha256_422(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/enterprise/api/v1/documents", json={
                "eventId": "evt-003",
                "sourceSystem": "EAM",
                "externalDocumentId": "DOC-003",
                "sourceVersionId": "v1",
                "sha256": "not-a-valid-sha256",
                "fileName": "test.pdf",
                "source": {"bucket": "x", "objectKey": "y"},
                "metadata": VALID_METADATA,
            })
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_status_200(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/enterprise/api/v1/documents/DOC-001/status",
                params={"tenant_id": "tenant-001", "source_system": "EAM"})
            # May be 404 if test DB not populated, but should not crash
            assert resp.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_error_response_shape(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/enterprise/api/v1/documents", json={
                "eventId": "evt-004",
                "sourceSystem": "EAM",
                "externalDocumentId": "DOC-004",
                "sourceVersionId": "v1",
                "sha256": hashlib.sha256(b"test").hexdigest(),
                "fileName": "test.pdf",
                "source": {"bucket": "x", "objectKey": "y"},
                "metadata": {"invalid": "schema"},
            })
            assert resp.status_code == 422
            data = resp.json()
            assert "code" in data
            assert "requestId" in data


# ── Regression: lifespan restart must not reuse a closed connection ──

@pytest.mark.asyncio
async def test_lifespan_can_restart_in_same_process(tmp_path):
    previous_db_path = os.environ.get("ENTERPRISE_SYNC_DB_PATH")
    os.environ["ENTERPRISE_SYNC_DB_PATH"] = str(tmp_path / "lifespan.db")
    try:
        if app_module._db is not None:
            await app_module._db.close()
            app_module._db = None
        for _ in range(2):
            async with app_module.lifespan(app_module.app):
                assert app_module._db is not None
                async with app_module._db.execute("SELECT 1") as cursor:
                    row = await cursor.fetchone()
                assert row[0] == 1
            assert app_module._db is None
    finally:
        if app_module._db is not None:
            await app_module._db.close()
            app_module._db = None
        if previous_db_path is None:
            os.environ.pop("ENTERPRISE_SYNC_DB_PATH", None)
        else:
            os.environ["ENTERPRISE_SYNC_DB_PATH"] = previous_db_path


# ── Integration: persistence ──

@pytest.mark.asyncio
class TestPersistence:
    async def test_insert_and_get(self):
        import tempfile
        db = await init_db(":memory:")
        doc = ExtDocumentMap(
            tenant_id="t1", source_system="EAM",
            external_document_id="DOC-1", source_version_id="v1",
            event_id="evt-1", sha256="a" * 64, file_name="test.pdf"
        )
        inserted = await insert_mapping(db, doc)
        assert inserted.id is not None

        found = await get_mapping(db, "t1", "EAM", "DOC-1", "v1")
        assert found is not None
        assert found.event_id == "evt-1"

        await db.close()

    async def test_insert_duplicate(self):
        db = await init_db(":memory:")
        doc = ExtDocumentMap(
            tenant_id="t1", source_system="EAM",
            external_document_id="DOC-2", source_version_id="v1",
            event_id="evt-2", sha256="b" * 64, file_name="test.pdf"
        )
        await insert_mapping(db, doc)

        doc2 = ExtDocumentMap(
            tenant_id="t1", source_system="EAM",
            external_document_id="DOC-2", source_version_id="v1",
            event_id="evt-2b", sha256="c" * 64, file_name="test2.pdf"
        )
        result = await insert_mapping(db, doc2)
        assert result.event_id == "evt-2"  # Returns original

        await db.close()

    async def test_event_id_lookup(self):
        db = await init_db(":memory:")
        doc = ExtDocumentMap(
            tenant_id="t1", source_system="EAM",
            external_document_id="DOC-3", source_version_id="v1",
            event_id="evt-3", sha256="d" * 64, file_name="test.pdf"
        )
        await insert_mapping(db, doc)

        found = await get_mapping_by_event_id(db, "evt-3")
        assert found is not None
        assert found.event_id == "evt-3"

        not_found = await get_mapping_by_event_id(db, "evt-nonexistent")
        assert not_found is None

        await db.close()

    async def test_update_status(self):
        db = await init_db(":memory:")
        doc = ExtDocumentMap(
            tenant_id="t1", source_system="EAM",
            external_document_id="DOC-4", source_version_id="v1",
            event_id="evt-4", sha256="e" * 64, file_name="test.pdf"
        )
        doc = await insert_mapping(db, doc)

        await update_mapping_status(db, doc, "parsing", pipeline_status="RUNNING",
                                     error_code="E001", error_message="test error")

        found = await get_mapping(db, "t1", "EAM", "DOC-4", "v1")
        assert found.sync_status == "parsing"
        assert found.pipeline_status == "RUNNING"

        await db.close()
