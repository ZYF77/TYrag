"""Demo closed-loop tests: upload PDF, status readiness, ask and citations."""
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.app import app  # noqa: E402
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    get_mapping,
    init_db,
    insert_mapping,
    update_mapping_status,
)


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "false")


@pytest_asyncio.fixture
async def isolated_demo_db(tmp_path):
    import enterprise.gateway.app as app_module
    import enterprise.gateway.demo as demo_module

    if app_module._db is not None:
        await app_module._db.close()
        app_module._db = None

    db = await init_db(str(tmp_path / "demo-gateway.db"))
    app_module.app.dependency_overrides[demo_module.get_db] = lambda: db
    try:
        yield db
    finally:
        app_module.app.dependency_overrides.pop(demo_module.get_db, None)
        await db.close()


@pytest.mark.usefixtures("isolated_demo_db")
class TestDemoUpload:
    @pytest.mark.asyncio
    async def test_upload_raw_pdf_persists_mapping(self, demo_env):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-001&tenantId=demo",
                content=b"%PDF-1.7 demo body",
                headers={
                    "Content-Type": "application/pdf",
                    "X-File-Name": "manual.pdf",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["externalDocumentId"] == "DOC-DEMO-001"
            assert body["status"] == "parsing"
            assert body["ragflowDatasetId"]
            assert body["ragflowDocumentId"]

    @pytest.mark.asyncio
    async def test_upload_duplicate_returns_deduplicated(
        self, isolated_demo_db, demo_env
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            url = (
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-002&tenantId=demo"
            )
            headers = {
                "Content-Type": "application/pdf",
                "X-File-Name": "manual.pdf",
            }
            first = await c.post(url, content=b"%PDF-1.7", headers=headers)
            second = await c.post(url, content=b"%PDF-1.7", headers=headers)
            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json()["deduplicated"] is True

    @pytest.mark.asyncio
    async def test_upload_rejects_non_pdf(self, demo_env):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-003&tenantId=demo",
                content=b"not pdf",
                headers={"Content-Type": "text/plain"},
            )
            assert resp.status_code == 422
            assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("isolated_demo_db")
class TestDemoAsk:
    @pytest.mark.asyncio
    async def test_ask_returns_answer_and_citations(self, isolated_demo_db, demo_env):
        doc = ExtDocumentMap(
            tenant_id="demo",
            source_system="DEMO",
            external_document_id="DOC-ASK-001",
            source_version_id="v1",
            event_id="evt-ask-001",
            sha256="a" * 64,
            file_name="manual.pdf",
            ragflow_dataset_id="ds-1",
            ragflow_document_id="doc-1",
            sync_status="ready",
        )
        doc = await insert_mapping(isolated_demo_db, doc)
        await update_mapping_status(
            isolated_demo_db, doc, "ready", pipeline_status="DONE"
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-ASK-001",
                    "tenantId": "demo",
                    "question": "如何排除故障码 E-104？",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "stub answer" in body["answer"]
            assert body["conversationId"]
            assert len(body["citations"]) == 1
            citation = body["citations"][0]
            assert citation["citationId"] == "chunk-1"
            assert citation["sourceType"] == "document"
            assert citation["documentId"] == "doc-1"
            assert citation["pageNo"] == 3
            assert citation["excerpt"] == "故障码 E-104 时先检查液压油位。"

    @pytest.mark.asyncio
    async def test_ask_persists_conversation_and_supports_followup(
        self, isolated_demo_db, demo_env
    ):
        doc = ExtDocumentMap(
            tenant_id="demo",
            source_system="DEMO",
            external_document_id="DOC-ASK-003",
            source_version_id="v1",
            event_id="evt-ask-003",
            sha256="c" * 64,
            file_name="manual.pdf",
            ragflow_dataset_id="ds-1",
            ragflow_document_id="doc-1",
            sync_status="ready",
        )
        doc = await insert_mapping(isolated_demo_db, doc)
        await update_mapping_status(
            isolated_demo_db, doc, "ready", pipeline_status="DONE"
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-ASK-003",
                    "tenantId": "demo",
                    "question": "问题一",
                },
            )
            assert first.status_code == 200
            conversation_id = first.json()["conversationId"]
            assert first.json()["ragflowSessionId"] == "stub-session"

            second = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-ASK-003",
                    "tenantId": "demo",
                    "conversationId": conversation_id,
                    "question": "问题二",
                },
            )
            assert second.status_code == 200
            assert second.json()["conversationId"] == conversation_id
            assert second.json()["ragflowSessionId"] == "stub-session"

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}"
            )
            assert history.status_code == 200
            messages = history.json()["messages"]
            assert len(messages) == 2
            assert messages[0]["question"] == "问题一"
            assert messages[1]["question"] == "问题二"
            assert messages[1]["citations"][0]["citationId"] == "chunk-1"

    @pytest.mark.asyncio
    async def test_ask_returns_404_for_unknown_conversation(
        self, isolated_demo_db, demo_env
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/enterprise/api/v1/demo/conversations/nope"
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "CONVERSATION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_ask_blocks_document_not_ready(
        self, isolated_demo_db, demo_env
    ):
        doc = ExtDocumentMap(
            tenant_id="demo",
            source_system="DEMO",
            external_document_id="DOC-ASK-002",
            source_version_id="v1",
            event_id="evt-ask-002",
            sha256="b" * 64,
            file_name="manual.pdf",
            ragflow_dataset_id="ds-1",
            ragflow_document_id="doc-1",
            sync_status="parsing",
        )
        doc = await insert_mapping(isolated_demo_db, doc)
        await update_mapping_status(
            isolated_demo_db, doc, "parsing", pipeline_status="RUNNING"
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-ASK-002",
                    "tenantId": "demo",
                    "question": "hello",
                },
            )
            assert resp.status_code == 409
            assert resp.json()["code"] == "DOCUMENT_NOT_READY"

    @pytest.mark.asyncio
    async def test_ask_returns_404_for_unknown_document(self, demo_env):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                json={
                    "externalDocumentId": "DOC-MISSING",
                    "tenantId": "demo",
                    "question": "hello",
                },
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_demo_db")
async def test_demo_routes_registered_before_catch_all(demo_env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/enterprise/api/v1/demo/ask",
            json={
                "externalDocumentId": "DOC-X",
                "tenantId": "demo",
                "question": "hello",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "DOCUMENT_NOT_FOUND"
