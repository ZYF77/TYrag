"""Query demo closed-loop tests with UserPrincipal and ACL ownership."""
import aiosqlite
import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.app import app  # noqa: E402
from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.query import acl_store  # noqa: E402
from enterprise.gateway.query import conversation_store  # noqa: E402
from enterprise.gateway.query import router as query_router  # noqa: E402
from enterprise.gateway.quality import models as quality_models  # noqa: E402
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    init_db,
    insert_mapping,
    update_mapping_status,
)

SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"
ROOT = Path(__file__).resolve().parents[2]


def _make_token(
    tenant: str = "customer-a",
    user: str = "biz-user-001",
    roles: tuple[str, ...] = ("end_user",),
) -> str:
    now = int(time.time())
    claims = {
        "sub": user,
        "tenant": tenant,
        "name": user,
        "department": ["d10"],
        "roles": list(roles),
        "groups": ["maintenance"],
        "security_level": 2,
        "iat": now - 60,
        "exp": now + 3600,
        "iss": "https://auth.example.com",
        "aud": "tyrag-gateway",
    }
    return jwt.encode(claims, SHARED_SECRET, algorithm="HS256")


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "query-demo.db")
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "false")
    monkeypatch.setenv("JWT_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("JWT_AUDIENCE", "tyrag-gateway")
    monkeypatch.setenv("JWT_ENABLE_HS", "true")
    monkeypatch.setenv("JWT_ALLOWED_ALGS", "HS256")
    monkeypatch.setenv("JWT_JWKS_URL", "")
    monkeypatch.setenv("JWT_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("ENTERPRISE_DB_PATH", db_path)
    monkeypatch.setenv("ENTERPRISE_SYNC_DB_PATH", db_path)
    return db_path


@pytest_asyncio.fixture
async def isolated_demo_db(demo_env):
    import enterprise.gateway.app as app_module
    import enterprise.gateway.query.router as query_router

    if app_module._db is not None:
        await app_module._db.close()
        app_module._db = None

    db = await init_db(demo_env)
    repo = ExtUserMapRepo(db_path=demo_env)
    await repo.ensure_table()
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id="customer-a",
            business_subject="biz-user-001",
            business_user_id="biz-user-001",
            mapping_strategy="B",
        )
    )
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id="customer-b",
            business_subject="biz-user-002",
            business_user_id="biz-user-002",
            mapping_strategy="B",
        )
    )
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id="customer-a",
            business_subject="biz-user-003",
            business_user_id="biz-user-003",
            mapping_strategy="B",
        )
    )
    await repo.close()
    query_router._query_stub = None
    app_module.app.dependency_overrides[query_router.get_db] = lambda: db
    try:
        yield db
    finally:
        app_module.app.dependency_overrides.pop(query_router.get_db, None)
        await db.close()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _insert_ready_document(
    db,
    *,
    doc_id: str,
    tenant_id: str = "customer-a",
    allowed_users: tuple[str, ...] = ("biz-user-001",),
    asset_id: str = "FA-DEMO-001",
):
    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system="DEMO",
        external_document_id=doc_id,
        source_version_id="v1",
        event_id=str(uuid.uuid4()),
        sha256=hashlib.sha256(b"ready").hexdigest(),
        file_name="manual.pdf",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-1",
        asset_id=asset_id,
        sync_status="ready",
    )
    doc = await insert_mapping(db, doc)
    await update_mapping_status(
        db, doc, "ready", pipeline_status="DONE"
    )
    for user in allowed_users:
        await acl_store.grant(
            db,
            tenant_id=tenant_id,
            external_document_id=doc_id,
            business_user_id=user,
        )
    return doc


@pytest.mark.usefixtures("isolated_demo_db")
class TestUploadAndStatus:
    @pytest.mark.asyncio
    async def test_upload_raw_pdf_and_status(self, isolated_demo_db):
        token = _make_token(roles=("knowledge_maintainer",))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-001",
                content=b"%PDF-1.7 demo body",
                headers={
                    **_headers(token),
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

            without_acl = await c.get(
                "/enterprise/api/v1/demo/documents/DOC-DEMO-001/status",
                headers=_headers(token),
            )
            assert without_acl.status_code == 403
            assert without_acl.json()["code"] == "ACL_DENIED"

            await acl_store.grant(
                isolated_demo_db,
                tenant_id="customer-a",
                external_document_id="DOC-DEMO-001",
                business_user_id="biz-user-001",
            )

            status = await c.get(
                "/enterprise/api/v1/demo/documents/DOC-DEMO-001/status",
                headers=_headers(token),
            )
            assert status.status_code == 200
            assert status.json()["status"] == "parsing"

    @pytest.mark.asyncio
    async def test_upload_duplicate_returns_deduplicated(self, isolated_demo_db):
        token = _make_token(roles=("knowledge_maintainer",))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            url = (
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-002"
            )
            headers = {
                **_headers(token),
                "Content-Type": "application/pdf",
                "X-File-Name": "manual.pdf",
            }
            first = await c.post(url, content=b"%PDF-1.7", headers=headers)
            assert first.status_code == 200
            await acl_store.grant(
                isolated_demo_db,
                tenant_id="customer-a",
                external_document_id="DOC-DEMO-002",
                business_user_id="biz-user-001",
            )
            second = await c.post(url, content=b"%PDF-1.7", headers=headers)
            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json()["deduplicated"] is True

    @pytest.mark.asyncio
    async def test_upload_rejects_non_pdf(self):
        token = _make_token(roles=("knowledge_maintainer",))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-003",
                content=b"not pdf",
                headers={**_headers(token), "Content-Type": "text/plain"},
            )
            assert resp.status_code == 422
            assert resp.json()["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_upload_requires_user_token(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-DEMO-004",
                content=b"%PDF-1.7",
                headers={"Content-Type": "application/pdf"},
            )
            assert resp.status_code == 401


@pytest.mark.usefixtures("isolated_demo_db")
class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_returns_answer_and_citations(self, isolated_demo_db):
        await _insert_ready_document(isolated_demo_db, doc_id="DOC-ASK-001")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-ASK-001",
                    "question": "如何排除故障码 E-104？",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "stub answer" in body["answer"]
            assert body["status"] == "completed"
            assert body["conversationId"]
            assert len(body["citations"]) == 1
            citation = body["citations"][0]
            assert citation["citationId"] == "chunk-1"
            assert citation["documentId"] == "doc-1"
            assert citation["versionId"] == "v1"
            assert citation["assetId"] == "FA-DEMO-001"
            assert citation["pageNo"] == 3

    @pytest.mark.asyncio
    async def test_ask_persists_conversation_and_supports_followup(
        self, isolated_demo_db
    ):
        await _insert_ready_document(isolated_demo_db, doc_id="DOC-ASK-003")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-ASK-003",
                    "question": "问题一",
                },
            )
            assert first.status_code == 200
            conversation_id = first.json()["conversationId"]
            ragflow_session_id = first.json()["ragflowSessionId"]
            assert ragflow_session_id.startswith("session-")

            second = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-ASK-003",
                    "conversationId": conversation_id,
                    "question": "问题二",
                },
            )
            assert second.status_code == 200
            assert second.json()["conversationId"] == conversation_id

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 200
            messages = history.json()["messages"]
            assert len(messages) == 4
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "问题一"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["status"] == "completed"
            assert messages[1]["citations"][0]["citationId"] == "chunk-1"
            assert messages[1]["citations"][0]["versionId"] == "v1"
            assert messages[1]["citations"][0]["assetId"] == "FA-DEMO-001"

    @pytest.mark.asyncio
    async def test_ask_returns_no_reliable_evidence_without_chunks(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-NO-EVIDENCE-001"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        query_router._query_stub._no_evidence = True
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-NO-EVIDENCE-001",
                    "question": "no evidence please",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["code"] == "NO_RELIABLE_EVIDENCE"
            assert body["status"] == "no_reliable_evidence"
            assert body["retryable"] is False
            assert body["answer"] == "未找到可靠依据，无法回答。"
            assert body["citations"] == []

            async with isolated_demo_db.execute(
                """SELECT COUNT(*), COALESCE(MAX(source_version_id), ''),
                          COALESCE(MAX(asset_id), ''),
                          COALESCE(MAX(ragflow_session_id), '')
                   FROM ext_conversation_map"""
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == 1
            assert row[1] == "v1"
            assert row[2] == "FA-DEMO-001"
            assert row[3] == body["ragflowSessionId"]

    @pytest.mark.asyncio
    async def test_ask_returns_no_reliable_evidence_when_answer_empty(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-EMPTY-ANSWER"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        query_router._query_stub._empty_answer = True
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-EMPTY-ANSWER",
                    "question": "empty answer please",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["code"] == "NO_RELIABLE_EVIDENCE"
            assert body["status"] == "no_reliable_evidence"
            assert len(body["citations"]) == 1

    @pytest.mark.asyncio
    async def test_ask_returns_no_reliable_evidence_when_chunks_empty(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-EMPTY-CHUNKS"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        query_router._query_stub._empty_chunks = True
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-EMPTY-CHUNKS",
                    "question": "empty chunks please",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["code"] == "NO_RELIABLE_EVIDENCE"
            assert body["status"] == "no_reliable_evidence"
            assert body["citations"] == []

    @pytest.mark.asyncio
    async def test_ask_returns_completed_with_empty_citations_when_chunks_unmapped(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-FILTERED-ALL"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        stub = query_router._query_stub
        stub._ignore_doc_scope = True
        stub._omit_default_chunk = True
        stub._extra_chunks.append(
            {
                "id": "chunk-9",
                "content": "未映射内容",
                "document_name": "other.pdf",
                "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
            }
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-FILTERED-ALL",
                    "question": "all chunks filtered",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "completed"
            assert body["citations"] == []

    @pytest.mark.asyncio
    async def test_ask_blocks_quality_review_required(
        self, isolated_demo_db, monkeypatch
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-QUALITY-REVIEW"
        )
        evaluation = await quality_models.get_or_create_evaluation(
            isolated_demo_db,
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-QUALITY-REVIEW",
            source_version_id="v1",
            ragflow_dataset_id="ds-1",
            ragflow_document_id="doc-1",
            routing={},
        )
        await isolated_demo_db.execute(
            """UPDATE parse_quality_evaluation
               SET evaluation_state='completed',
                   parse_quality_status='review_required',
                   quality_reasons='["REVIEW"]'
               WHERE id=?""",
            (evaluation.id,),
        )
        await isolated_demo_db.commit()
        monkeypatch.setenv("ENTERPRISE_QUALITY_GATE_ENABLED", "true")

        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-QUALITY-REVIEW",
                    "question": "hello",
                },
            )
            assert resp.status_code == 409
            assert resp.json()["code"] == "DOCUMENT_REVIEW_REQUIRED"

    @pytest.mark.asyncio
    async def test_citation_keeps_missing_asset_null(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-MISSING-ASSET", asset_id=None
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-MISSING-ASSET",
                    "question": "hello",
                },
            )
            assert resp.status_code == 200
            citation = resp.json()["citations"][0]
            assert citation["versionId"] == "v1"
            assert citation["assetId"] is None

    @pytest.mark.asyncio
    async def test_ask_blocks_document_not_ready(self, isolated_demo_db):
        doc = ExtDocumentMap(
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-ASK-002",
            source_version_id="v1",
            event_id=str(uuid.uuid4()),
            sha256=hashlib.sha256(b"parsing").hexdigest(),
            file_name="manual.pdf",
            ragflow_dataset_id="ds-1",
            ragflow_document_id="doc-1",
            sync_status="parsing",
        )
        doc = await insert_mapping(isolated_demo_db, doc)
        await update_mapping_status(
            isolated_demo_db, doc, "parsing", pipeline_status="RUNNING"
        )
        await acl_store.grant(
            isolated_demo_db,
            tenant_id="customer-a",
            external_document_id="DOC-ASK-002",
            business_user_id="biz-user-001",
        )

        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-ASK-002",
                    "question": "hello",
                },
            )
            assert resp.status_code == 409
            assert resp.json()["code"] == "DOCUMENT_NOT_READY"

    @pytest.mark.asyncio
    async def test_ask_passes_authorized_doc_ids_and_drops_unauthorized_chunks(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-SCOPE-001"
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-SCOPE-001",
                    "question": "第一问",
                },
            )
            assert first.status_code == 200
            stub = query_router._query_stub
            assert stub._last_completion_body["doc_ids"] == "doc-1"

            stub._extra_chunks.append(
                {
                    "id": "chunk-2",
                    "content": "未授权文档内容",
                    "document_id": "doc-2",
                    "document_name": "other.pdf",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                }
            )
            conversation_id = first.json()["conversationId"]
            second = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-SCOPE-001",
                    "conversationId": conversation_id,
                    "question": "第二问",
                },
            )
            assert second.status_code == 200
            assert [
                c["documentId"] for c in second.json()["citations"]
            ] == ["doc-1"]

    @pytest.mark.asyncio
    async def test_ask_fails_closed_when_ragflow_returns_out_of_scope_chunk(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-SCOPE-002"
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-SCOPE-002",
                    "question": "hello",
                },
            )
            assert first.status_code == 200
            stub = query_router._query_stub
            stub._ignore_doc_scope = True
            stub._extra_chunks.append(
                {
                    "id": "chunk-9",
                    "content": "越权内容",
                    "document_id": "doc-9",
                    "document_name": "other.pdf",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                }
            )
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-SCOPE-002",
                    "conversationId": first.json()["conversationId"],
                    "question": "hello again",
                },
            )
            assert resp.status_code == 502
            assert resp.json()["code"] == "RAGFLOW_SCOPE_VIOLATION"

    @pytest.mark.asyncio
    async def test_ask_returns_404_for_unknown_document(self):
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-MISSING",
                    "question": "hello",
                },
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.usefixtures("isolated_demo_db")
class TestCapabilities:
    @pytest.mark.asyncio
    async def test_read_only_user_cannot_upload(self):
        token = _make_token(user="biz-user-003", roles=())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/documents"
                "?externalDocumentId=DOC-RO-001",
                content=b"%PDF-1.7",
                headers={
                    **_headers(token),
                    "Content-Type": "application/pdf",
                    "X-File-Name": "manual.pdf",
                },
            )
            assert resp.status_code == 403
            assert resp.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_read_only_user_cannot_ask(self, isolated_demo_db):
        await _insert_ready_document(
            isolated_demo_db,
            doc_id="DOC-RO-002",
            allowed_users=("biz-user-003",),
        )
        token = _make_token(user="biz-user-003", roles=())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-RO-002",
                    "question": "hello",
                },
            )
            assert resp.status_code == 403
            assert resp.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_read_only_user_cannot_read_conversation(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db,
            doc_id="DOC-RO-003",
            allowed_users=("biz-user-001",),
        )
        end_user_token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(end_user_token),
                json={
                    "externalDocumentId": "DOC-RO-003",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]

            read_only_token = _make_token(roles=())
            resp = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(read_only_token),
            )
            assert resp.status_code == 403
            assert resp.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_demo_routes_disabled_return_404(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_DEMO_ROUTES_ENABLED", "false")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-DISABLED",
                    "question": "hello",
                },
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "REQUEST_FAILED"


@pytest.mark.usefixtures("isolated_demo_db")
class TestConversationUnavailable:
    @pytest.mark.asyncio
    async def test_returns_503_when_ragflow_session_read_fails(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-CONV-UNAVAILABLE"
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-CONV-UNAVAILABLE",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]

            query_router._query_stub._fail_session_read = True
            resp = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert resp.status_code == 503
            assert resp.json()["code"] == "CONVERSATION_UNAVAILABLE"
            assert resp.json()["retryable"] is True


@pytest.mark.usefixtures("isolated_demo_db")
class TestHistoryEmptyMessageFilter:
    @pytest.mark.asyncio
    async def test_no_reliable_evidence_is_replayed_with_business_status(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-NO-EVIDENCE-HISTORY"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        query_router._query_stub._no_evidence = True
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-NO-EVIDENCE-HISTORY",
                    "question": "no evidence please",
                },
            )
            assert created.status_code == 200
            assert created.json()["code"] == "NO_RELIABLE_EVIDENCE"
            assert created.json()["status"] == "no_reliable_evidence"
            conversation_id = created.json()["conversationId"]

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 200
            messages = history.json()["messages"]
            assistant = next(
                m
                for m in messages
                if m["role"] == "assistant"
                and m["status"] == "no_reliable_evidence"
            )
            assert assistant["content"].strip()
            assert assistant["citations"] == []

    @pytest.mark.asyncio
    async def test_no_reliable_evidence_replays_with_citations_when_chunks_exist(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-NO-EVIDENCE-WITH-CHUNKS"
        )
        query_router._query_stub = query_router.RAGFlowQueryStub()
        query_router._query_stub._empty_answer = True
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-NO-EVIDENCE-WITH-CHUNKS",
                    "question": "empty answer please",
                },
            )
            assert created.status_code == 200
            assert created.json()["status"] == "no_reliable_evidence"
            assert created.json()["citations"]
            conversation_id = created.json()["conversationId"]

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 200
            assistant = next(
                m
                for m in history.json()["messages"]
                if m["role"] == "assistant"
                and m["status"] == "no_reliable_evidence"
            )
            assert assistant["content"].strip()
            assert assistant["citations"]

    @pytest.mark.asyncio
    async def test_empty_assistant_message_is_not_replayed(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-EMPTY-HISTORY"
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-EMPTY-HISTORY",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]
            ragflow_session_id = created.json()["ragflowSessionId"]

            stub = query_router._query_stub
            stub._sessions[ragflow_session_id]["messages"].append(
                {"role": "assistant", "content": ""}
            )
            stub._sessions[ragflow_session_id]["reference"].append(
                {"chunks": []}
            )

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 200
            messages = history.json()["messages"]
            assert any(
                m["role"] == "assistant"
                and m["content"]
                and m["citations"]
                for m in messages
            )
            assert not any(
                m["role"] == "assistant"
                and not m["content"].strip()
                and not m["citations"]
                for m in messages
            )


@pytest.mark.asyncio
async def test_ensure_schema_preserves_old_content_and_citations_columns(
    tmp_path,
):
    db = await aiosqlite.connect(str(tmp_path / "migration.db"))
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE ext_conversation_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            business_conversation_id TEXT NOT NULL,
            business_user_id TEXT NOT NULL,
            ragflow_chat_id TEXT,
            ragflow_session_id TEXT,
            external_document_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_message_at TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE ext_conversation_message (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            business_user_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            content TEXT,
            citations_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        """INSERT INTO ext_conversation_map
           (tenant_id, business_conversation_id, business_user_id,
            ragflow_chat_id, ragflow_session_id, external_document_id,
            status, created_at, last_message_at)
           VALUES ('t', 'c', 'u', 'chat', 'session', 'doc',
                   'active', 'now', 'now')"""
    )
    await db.execute(
        """INSERT INTO ext_conversation_message
           (conversation_id, tenant_id, business_user_id, message_id,
            role, status, content, citations_json, created_at, updated_at)
           VALUES ('c', 't', 'u', 'm', 'assistant', 'completed',
                   '正文', '[]', 'now', 'now')"""
    )
    await db.commit()

    await conversation_store.ensure_schema(db)
    await conversation_store.ensure_schema(db)

    async with db.execute(
        "PRAGMA table_info(ext_conversation_message)"
    ) as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    assert "content" in columns
    assert "citations_json" in columns
    assert "ragflow_message_id" in columns

    async with db.execute(
        """SELECT content, citations_json
           FROM ext_conversation_message
           WHERE message_id='m'"""
    ) as cursor:
        row = await cursor.fetchone()
    assert row["content"] == "正文"
    assert row["citations_json"] == "[]"
    await db.close()


@pytest.mark.usefixtures("isolated_demo_db")
class TestCitationSnapshot:
    @pytest.mark.asyncio
    async def test_history_keeps_conversation_asset_snapshot(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db, doc_id="DOC-SNAP-001"
        )
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-SNAP-001",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]

            await isolated_demo_db.execute(
                """UPDATE ext_document_map
                   SET asset_id='FA-CHANGED'
                   WHERE external_document_id='DOC-SNAP-001'"""
            )
            await isolated_demo_db.commit()

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 200
            citations = history.json()["messages"][1]["citations"]
            assert citations
            assert all(
                c["assetId"] == "FA-DEMO-001" for c in citations
            )


@pytest.mark.usefixtures("isolated_demo_db")
class TestOwnership:
    @pytest.mark.asyncio
    async def test_user_cannot_read_other_users_conversation(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db,
            doc_id="DOC-OWN-001",
            allowed_users=("biz-user-001", "biz-user-003"),
        )
        token_a = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_a),
                json={
                    "externalDocumentId": "DOC-OWN-001",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]

            token_b = _make_token(user="biz-user-003")
            resp = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token_b),
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "CONVERSATION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_user_cannot_continue_other_users_conversation(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db,
            doc_id="DOC-OWN-002",
            allowed_users=("biz-user-001", "biz-user-003"),
        )
        token_a = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_a),
                json={
                    "externalDocumentId": "DOC-OWN-002",
                    "question": "hello",
                },
            )
            conversation_id = created.json()["conversationId"]

            token_b = _make_token(user="biz-user-003")
            resp = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_b),
                json={
                    "externalDocumentId": "DOC-OWN-002",
                    "conversationId": conversation_id,
                    "question": "hi",
                },
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == "CONVERSATION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_user_cannot_access_other_users_document(
        self, isolated_demo_db
    ):
        await _insert_ready_document(isolated_demo_db, doc_id="DOC-OWN-003")
        token_b = _make_token(user="biz-user-003")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            status = await c.get(
                "/enterprise/api/v1/demo/documents/DOC-OWN-003/status",
                headers=_headers(token_b),
            )
            assert status.status_code == 403
            assert status.json()["code"] == "ACL_DENIED"

            ask = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_b),
                json={
                    "externalDocumentId": "DOC-OWN-003",
                    "question": "hello",
                },
            )
            assert ask.status_code == 403
            assert ask.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_same_conversation_id_is_scoped_per_user(
        self, isolated_demo_db
    ):
        await _insert_ready_document(
            isolated_demo_db,
            doc_id="DOC-OWN-004",
            allowed_users=("biz-user-001", "biz-user-003"),
        )
        token_a = _make_token()
        token_b = _make_token(user="biz-user-003")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created_a = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_a),
                json={
                    "externalDocumentId": "DOC-OWN-004",
                    "question": "user A",
                },
            )
            assert created_a.status_code == 200
            conversation_id = created_a.json()["conversationId"]
            session_a = created_a.json()["ragflowSessionId"]

            created_b = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token_b),
                json={
                    "externalDocumentId": "DOC-OWN-004",
                    "conversationId": conversation_id,
                    "question": "user B",
                },
            )
            assert created_b.status_code == 404
            assert (
                created_b.json()["code"] == "CONVERSATION_NOT_FOUND"
            )

            history_a = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token_a),
            )
            assert history_a.status_code == 200
            assert history_a.json()["ragflowSessionId"] == session_a

            async with isolated_demo_db.execute(
                "SELECT COUNT(*) AS n FROM ext_conversation_map "
                "WHERE business_conversation_id=?",
                (conversation_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_history_rechecks_document_acl(self, isolated_demo_db):
        await _insert_ready_document(isolated_demo_db, doc_id="DOC-OWN-005")
        token = _make_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/enterprise/api/v1/demo/ask",
                headers=_headers(token),
                json={
                    "externalDocumentId": "DOC-OWN-005",
                    "question": "hello",
                },
            )
            assert created.status_code == 200
            conversation_id = created.json()["conversationId"]

            await isolated_demo_db.execute(
                "DELETE FROM demo_document_acl "
                "WHERE tenant_id='customer-a' "
                "AND external_document_id='DOC-OWN-005'"
            )
            await isolated_demo_db.commit()

            history = await c.get(
                f"/enterprise/api/v1/demo/conversations/{conversation_id}",
                headers=_headers(token),
            )
            assert history.status_code == 403
            assert history.json()["code"] == "ACL_DENIED"


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_demo_db")
async def test_query_routes_registered_before_catch_all():
    token = _make_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/enterprise/api/v1/demo/ask",
            headers=_headers(token),
            json={
                "externalDocumentId": "DOC-X",
                "question": "hello",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_root_endpoint_returns_service_info():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "Enterprise RAGFlow Gateway"
        assert body["docs"] == "/docs"


def test_demo_routes_absent_in_production_subprocess():
    env = {
        **os.environ,
        "ENTERPRISE_TEST_MODE": "0",
        "ENTERPRISE_DEMO_ROUTES_ENABLED": "0",
    }
    code = (
        "from enterprise.gateway.app import app; "
        "from enterprise.gateway.query import router; "
        "print(any(getattr(r, 'original_router', None) is router.router "
        "for r in app.routes))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    registered = result.stdout.strip().splitlines()[-1] == "True"
    assert not registered


def test_demo_routes_present_when_explicitly_enabled_subprocess():
    env = {
        **os.environ,
        "ENTERPRISE_TEST_MODE": "0",
        "ENTERPRISE_DEMO_ROUTES_ENABLED": "1",
    }
    code = (
        "from enterprise.gateway.app import app; "
        "from enterprise.gateway.query import router; "
        "print(any(getattr(r, 'original_router', None) is router.router "
        "for r in app.routes))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    registered = result.stdout.strip().splitlines()[-1] == "True"
    assert registered
