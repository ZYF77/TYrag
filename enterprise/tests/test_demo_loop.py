"""Query demo closed-loop tests with UserPrincipal and ACL ownership."""
import hashlib
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
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    init_db,
    insert_mapping,
    update_mapping_status,
)

SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"


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
    async def test_upload_raw_pdf_and_status(self):
        token = _make_token()
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

            status = await c.get(
                "/enterprise/api/v1/demo/documents/DOC-DEMO-001/status",
                headers=_headers(token),
            )
            assert status.status_code == 200
            assert status.json()["status"] == "parsing"

    @pytest.mark.asyncio
    async def test_upload_duplicate_returns_deduplicated(self):
        token = _make_token()
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
            second = await c.post(url, content=b"%PDF-1.7", headers=headers)
            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json()["deduplicated"] is True

    @pytest.mark.asyncio
    async def test_upload_rejects_non_pdf(self):
        token = _make_token()
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
            assert body["conversationId"]
            assert len(body["citations"]) == 1
            citation = body["citations"][0]
            assert citation["citationId"] == "chunk-1"
            assert citation["documentId"] == "doc-1"
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
            assert first.json()["ragflowSessionId"] == "stub-session"

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
            assert messages[1]["citations"][0]["citationId"] == "chunk-1"

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
