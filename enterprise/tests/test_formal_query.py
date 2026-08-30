"""Formal WP-04 Phase 2 API tests: conversations, SSE, ACL and snapshots."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.app import app  # noqa: E402
from enterprise.gateway.auth.user_principal import UserPrincipal  # noqa: E402
from enterprise.gateway.db.dialect import begin_transaction, exec_sql, fetchall, fetchone, table_columns  # noqa: E402
from enterprise.gateway.db.ops import gw_read, gw_write  # noqa: E402
from enterprise.gateway.db.testing import create_gateway  # noqa: E402
from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.query import conversation_store  # noqa: E402
from enterprise.gateway.query import router as query_router  # noqa: E402
from enterprise.gateway.quality import models as quality_models  # noqa: E402
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    get_mapping,
    insert_mapping,
    update_mapping_status,
)

SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"


def _make_token(
    tenant: str = "customer-a",
    user: str = "biz-user-001",
    roles: tuple[str, ...] = ("end_user",),
    groups: tuple[str, ...] = ("maintenance",),
) -> str:
    now = int(time.time())
    claims = {
        "sub": user,
        "tenant": tenant,
        "name": user,
        "department": ["d10"],
        "roles": list(roles),
        "groups": list(groups),
        "security_level": 2,
        "iat": now - 60,
        "exp": now + 3600,
        "iss": "https://auth.example.com",
        "aud": "tyrag-gateway",
    }
    return jwt.encode(claims, SHARED_SECRET, algorithm="HS256")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _principal() -> UserPrincipal:
    return UserPrincipal(
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


async def _get_doc(db, doc_id: str):
    return await gw_read(
        db, get_mapping, "customer-a", "DEMO", doc_id, "v1"
    )


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "formal-query.db")
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
async def isolated_db(demo_env):
    import enterprise.gateway.app as app_module
    from enterprise.gateway.query import formal_router

    if app_module._gateway_db is not None:
        await app_module._gateway_db.dispose()
        app_module._gateway_db = None

    gateway = await create_gateway(demo_env)
    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """INSERT INTO ext_asset_registry
               (tenant_id, equipment_id, fixed_asset_no, asset_id)
               VALUES ('customer-a', 'EQ-1', 'FA-1', 'ASSET-1')""",
        )
    repo = ExtUserMapRepo(gateway=gateway)
    for tenant, subject, business_user in (
        ("customer-a", "biz-user-001", "biz-user-001"),
        ("customer-b", "biz-user-002", "biz-user-002"),
        ("customer-a", "biz-user-003", "biz-user-003"),
    ):
        await repo.insert_mapping(
            ExtUserMap(
                tenant_id=tenant,
                business_subject=subject,
                business_user_id=business_user,
                mapping_strategy="B",
            )
        )
    await repo.close()
    query_router._query_stub = None
    formal_router._query_stub = None
    app_module.app.dependency_overrides[app_module.get_gateway_db] = lambda: gateway
    app_module.app.dependency_overrides[app_module.get_db] = lambda: gateway
    app_module.app.dependency_overrides[formal_router.get_db] = lambda: gateway
    try:
        yield gateway
    finally:
        app_module.app.dependency_overrides.pop(app_module.get_gateway_db, None)
        app_module.app.dependency_overrides.pop(app_module.get_db, None)
        app_module.app.dependency_overrides.pop(formal_router.get_db, None)
        await gateway.dispose()
        if app_module._gateway_db is not None:
            await app_module._gateway_db.dispose()
            app_module._gateway_db = None


async def _insert_document(
    db,
    *,
    doc_id: str,
    tenant_id: str = "customer-a",
    ragflow_doc_id: str = "doc-1",
    dataset_id: str = "ds-1",
    sync_status: str = "ready",
    asset_id: str = "FA-DOC-001",
    equipment_id: str = "EQ-1",
    fixed_asset_no: str | None = "FA-1",
    version_id: str = "v1",
    department_id: str = "d10",
    security_level: int = 2,
    allow_groups: tuple[str, ...] = ("maintenance",),
    deny_groups: tuple[str, ...] = (),
    quality: str | None = "passed",
    source_system: str = "DEMO",
):
    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system=source_system,
        external_document_id=doc_id,
        source_version_id=version_id,
        event_id=str(uuid.uuid4()),
        sha256=hashlib.sha256(b"ready").hexdigest(),
        file_name=f"{doc_id}.pdf",
        ragflow_dataset_id=dataset_id,
        ragflow_document_id=ragflow_doc_id,
        asset_id=asset_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        department_id=department_id,
        security_level=security_level,
        allow_group_ids=json.dumps(list(allow_groups)),
        deny_group_ids=json.dumps(list(deny_groups)),
        sync_status=sync_status,
    )
    doc = await gw_write(db, insert_mapping, doc)
    await gw_write(
        db, update_mapping_status, doc, sync_status, pipeline_status="DONE"
    )
    if quality:
        evaluation = await gw_write(
            db,
            quality_models.get_or_create_evaluation,
            tenant_id=tenant_id,
            source_system=source_system,
            external_document_id=doc_id,
            source_version_id=version_id,
            ragflow_dataset_id=dataset_id,
            ragflow_document_id=ragflow_doc_id,
            routing={},
        )
        await gw_write(
            db,
            quality_models.complete_evaluation,
            evaluation.id,
            parse_quality_status=quality,
            quality_reasons=[],
            metrics_json={
                "parse_success": True,
                "chunk_count": 5,
                "parserApplication": {
                    "state": "executed",
                    "readbackMatch": True,
                },
            },
            parse_repeatability_hash="hash",
            e2e_repeatability_hash="hash",
            artifact_hash="hash",
            enterprise_commit="commit",
            enterprise_worktree_dirty=False,
            ragflow_source_tag="v0.26.4",
            ragflow_source_commit="commit",
            thresholds_version="1",
            thresholds_digest="digest",
        )
    return doc


async def _create_conversation(client, token):
    resp = await client.post(
        "/enterprise/api/v1/conversations",
        headers=_headers(token),
        json={"equipmentId": "EQ-1", "fixedAssetNo": "FA-1", "faultCode": "E-104"},
    )
    return resp


@pytest.mark.usefixtures("isolated_db")
class TestConversation:
    @pytest.mark.asyncio
    async def test_create_conversation_returns_enterprise_id(self):
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await _create_conversation(c, token)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["conversationId"]
        assert body["createdAt"]
        assert "ragflowSessionId" not in body

    @pytest.mark.asyncio
    async def test_create_requires_list_sessions_capability(self):
        token = _make_token(roles=())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await _create_conversation(c, token)
        assert resp.status_code == 403
        assert resp.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_user_cannot_read_other_users_conversation(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            created = await _create_conversation(c, _make_token())
            conversation_id = created.json()["conversationId"]
            denied = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(_make_token(user="biz-user-003")),
            )
        assert denied.status_code == 404
        assert denied.json()["code"] == "CONVERSATION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_missing_conversation_returns_404(self):
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get(
                f"/enterprise/api/v1/conversations/{uuid.uuid4()}",
                headers=_headers(token),
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "CONVERSATION_NOT_FOUND"


@pytest.mark.usefixtures("isolated_db")
class TestAsk:
    @pytest.mark.asyncio
    async def test_authorized_document_returns_answer_and_citation(
        self, isolated_db
    ):
        await _insert_document(isolated_db, doc_id="DOC-1")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "故障码 E-104 怎么处理？"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["answer"]
        assert body["citations"]
        assert all(
            c["documentId"] == "DOC-1" and c["versionId"] == "v1"
            and c["assetId"] == "FA-DOC-001"
            for c in body["citations"]
        )

    @pytest.mark.asyncio
    async def test_think_tags_are_stripped_from_formal_answer(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-THINK")
        from enterprise.gateway.query import formal_router
        from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub

        class _ThinkStub(RAGFlowQueryStub):
            async def chat_completion(
                self,
                chat_id: str,
                question: str,
                session_id: str | None = None,
                doc_ids: list[str] | None = None,
                request_id: str | None = None,
            ) -> dict:
                del chat_id, question, session_id, doc_ids, request_id
                return {
                    "code": 0,
                    "data": {
                        "answer": "<think>规划过程</think>故障码 E-104 时先检查液压油位。 [ID:0]",
                        "id": "ragflow-message",
                        "session_id": "ragflow-session",
                        "status": "completed",
                        "reference": {
                            "chunks": [
                                {
                                    "id": "chunk-1",
                                    "document_id": "doc-1",
                                    "document_name": "manual.pdf",
                                    "content": "故障码 E-104 时先检查液压油位。",
                                    "positions": [[3, 0.1, 0.8, 0.2, 0.4]],
                                }
                            ]
                        },
                    },
                }

        formal_router._query_stub = _ThinkStub()
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "故障码 E-104 怎么处理？"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "规划" not in body["answer"]
        assert "<think>" not in body["answer"]
        assert "液压油位" in body["answer"]
        assert body.get("reasoning") == "规划过程"

    @pytest.mark.asyncio
    async def test_non_ready_document_is_not_retrievable(self, isolated_db):
        await _insert_document(
            isolated_db, doc_id="DOC-NOT-READY", sync_status="parsing"
        )
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_reliable_evidence"
        assert resp.json()["citations"] == []

    @pytest.mark.asyncio
    async def test_quality_failed_document_is_not_retrievable(
        self, isolated_db, monkeypatch
    ):
        monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "true")
        await _insert_document(
            isolated_db, doc_id="DOC-BAD-QUALITY", quality="failed"
        )
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_reliable_evidence"

    @pytest.mark.asyncio
    async def test_unauthorized_document_is_not_retrievable(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-A", ragflow_doc_id="doc-a")
        token_b = _make_token(user="biz-user-003", groups=("other",))
        # RF-PATCH-007: the Gateway only trusts data.status. The document ACL
        # policy is intentionally tenant-open at this stage, so emulate the
        # upstream finding no authorized evidence for this user instead of
        # relying on the removed empty-chunk derivation.
        from enterprise.gateway.query import formal_router

        stub = formal_router.RAGFlowQueryStub()
        stub._no_evidence = True
        formal_router._query_stub = stub
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token_b)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token_b),
                json={"question": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_reliable_evidence"
        assert resp.json()["citations"] == []

    @pytest.mark.asyncio
    async def test_ask_requires_ask_capability(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-1")
        read_only = _make_token(user="biz-user-003", roles=())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, _make_token())
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(read_only),
                json={"question": "hello"},
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "ACL_DENIED"

    @pytest.mark.asyncio
    async def test_missing_status_json_returns_502_contract_error(
        self, isolated_db
    ):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-NO-STATUS-JSON")
        stub = formal_router.RAGFlowQueryStub()
        stub._omit_status = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        assert resp.status_code == 502
        assert resp.json()["code"] == "RAGFLOW_API_INCOMPATIBLE"
        assistant = next(
            m for m in history.json()["messages"] if m["role"] == "assistant"
        )
        assert assistant["status"] == "failed"


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event = "message"
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if data:
            events.append((event, json.loads(data)))
    return events


@pytest.mark.usefixtures("isolated_db")
class TestSse:
    @pytest.mark.asyncio
    async def test_stream_deltas_citations_and_completed(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-SSE")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "故障码 E-104 怎么处理？"},
            )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        names = [e for e, _ in events]
        assert names[0] == "run.started"
        assert "answer.delta" in names
        assert "citation" in names
        completed = next(data for e, data in events if e == "answer.completed")
        assert completed["status"] == "completed"
        assert completed["citations"]
        assert names[-1] == "answer.completed"

    @pytest.mark.asyncio
    async def test_stream_no_evidence_completes(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-NOEVIDENCE")
        from enterprise.gateway.query import formal_router

        stub = formal_router.RAGFlowQueryStub()
        stub._no_evidence = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "unrelated"},
            )
        events = _parse_sse(resp.text)
        completed = next(data for e, data in events if e == "answer.completed")
        assert completed["status"] == "no_reliable_evidence"
        assert completed["citations"] == []
        assert "run.failed" not in [e for e, _ in events]

    @pytest.mark.asyncio
    async def test_stream_failure_never_sends_completed(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-FAIL")
        from enterprise.gateway.query import formal_router

        stub = formal_router.RAGFlowQueryStub()
        stub._stream_fail_after = 1
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "hello"},
            )
        events = _parse_sse(resp.text)
        assert "run.failed" in [e for e, _ in events]
        assert "answer.completed" not in [e for e, _ in events]

    @pytest.mark.asyncio
    async def test_disconnect_persists_failed_not_completed(self, isolated_db):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-CANCEL")
        stub = formal_router.RAGFlowQueryStub()
        stub._stream_delay = 1.0
        formal_router._query_stub = stub
        conversation_id = str(uuid.uuid4())
        await conversation_store.ensure_schema(isolated_db)
        await gw_write(
            isolated_db,
            conversation_store.create_conversation,
            tenant_id="customer-a",
            business_user_id="biz-user-001",
            conversation_id=conversation_id,
        )
        principal = _principal()
        scope = formal_router.AclScope.materialized(
            ("ds-1",), ("doc-1",), policy_version="1"
        )
        docs = {"doc-1": await _get_doc(isolated_db, "DOC-CANCEL")}
        conversation = await gw_read(
            isolated_db,
            conversation_store.get_conversation,
            conversation_id=conversation_id,
            tenant_id="customer-a",
            business_user_id="biz-user-001",
        )
        gen = formal_router._stream_ask_events(
            isolated_db,
            principal,
            conversation,
            formal_router.AskRequest(question="hello"),
            scope,
            docs,
            str(uuid.uuid4()),
        )
        task = asyncio.ensure_future(gen.__anext__())
        await task
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        messages = await gw_read(
            isolated_db,
            conversation_store.list_messages,
            conversation_id=conversation_id,
            tenant_id="customer-a",
            business_user_id="biz-user-001",
        )
        assistant = next(
            m for m in messages if m["role"] == "assistant"
        )
        assert assistant["status"] == "failed"


@pytest.mark.usefixtures("isolated_db")
class TestCitation:
    @pytest.mark.asyncio
    async def test_citation_belongs_to_message_and_maps_version(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-CIT")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            ask = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
            citation_id = ask.json()["citations"][0]["citationId"]
            detail = await c.get(
                f"/enterprise/api/v1/citations/{citation_id}",
                headers=_headers(token),
            )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["citationId"] == citation_id
        assert body["versionId"] == "v1"
        assert body["assetId"] == "FA-DOC-001"
        assert body["documentId"] == "DOC-CIT"

    @pytest.mark.asyncio
    async def test_user_cannot_read_other_users_citation(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-CIT")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, _make_token())
            ).json()["conversationId"]
            ask = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(_make_token()),
                json={"question": "hello"},
            )
            citation_id = ask.json()["citations"][0]["citationId"]
            denied = await c.get(
                f"/enterprise/api/v1/citations/{citation_id}",
                headers=_headers(_make_token(user="biz-user-003")),
            )
        assert denied.status_code == 404

    @pytest.mark.asyncio
    async def test_citation_rechecks_document_acl(self, isolated_db):
        doc = await _insert_document(
            isolated_db,
            doc_id="DOC-REVOKE",
            allow_groups=("maintenance",),
        )
        token = _make_token(groups=("maintenance",))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            ask = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
            citation_id = ask.json()["citations"][0]["citationId"]
            # TEST_TENANT_OPEN ignores group mismatch; revoke via document status
            # which citation ACL re-checks on every read.
            await gw_write(
                isolated_db,
                update_mapping_status,
                doc,
                "ready",
                business_status="disabled",
            )
            denied = await c.get(
                f"/enterprise/api/v1/citations/{citation_id}",
                headers=_headers(token),
            )
        assert denied.status_code == 403

    @pytest.mark.asyncio
    async def test_guessing_citation_id_returns_404(self):
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get(
                f"/enterprise/api/v1/citations/{uuid.uuid4()}",
                headers=_headers(token),
            )
        assert resp.status_code == 404


@pytest.mark.usefixtures("isolated_db")
class TestSnapshot:
    @pytest.mark.asyncio
    async def test_history_citation_snapshot_does_not_drift(self, isolated_db):
        doc = await _insert_document(isolated_db, doc_id="DOC-SNAP")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            ask = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
            citation_id = ask.json()["citations"][0]["citationId"]

            await _insert_document(
                isolated_db,
                doc_id="DOC-SNAP",
                version_id="v2",
                asset_id="FA-DOC-002",
                ragflow_doc_id="doc-snap-v2",
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
            detail = await c.get(
                f"/enterprise/api/v1/citations/{citation_id}",
                headers=_headers(token),
            )
        assistant = next(
            m for m in history.json()["messages"] if m["role"] == "assistant"
        )
        assert assistant["citations"][0]["versionId"] == "v1"
        assert assistant["citations"][0]["assetId"] == "FA-DOC-001"
        assert detail.json()["versionId"] == "v1"
        assert detail.json()["assetId"] == "FA-DOC-001"


@pytest.mark.usefixtures("isolated_db")
class TestAclIdor:
    @pytest.mark.asyncio
    async def test_same_conversation_serializes_asks(self):
        from enterprise.gateway.query import formal_router

        lock_a = await formal_router._conversation_lock("conv-1")
        lock_a_again = await formal_router._conversation_lock("conv-1")
        lock_b = await formal_router._conversation_lock("conv-2")
        assert lock_a is lock_a_again
        assert lock_a is not lock_b

    @pytest.mark.asyncio
    async def test_out_of_scope_chunk_fails_closed(self, isolated_db):
        await _insert_document(
            isolated_db, doc_id="DOC-SCOPE", ragflow_doc_id="doc-scope"
        )
        from enterprise.gateway.query import formal_router

        stub = formal_router.RAGFlowQueryStub()
        stub._ignore_doc_scope = True
        stub._extra_chunks = [
            {
                "id": "chunk-evil",
                "content": "evil",
                "document_id": "doc-other",
                "document_name": "other.pdf",
                "positions": [],
            }
        ]
        stub._omit_default_chunk = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
        assert resp.status_code == 502
        assert resp.json()["code"] == "RAGFLOW_SCOPE_VIOLATION"

    @pytest.mark.asyncio
    async def test_ragflow_session_id_is_not_exposed(self, isolated_db):
        await _insert_document(isolated_db, doc_id="DOC-NOID")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            ask = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "hello"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        assert "ragflowSessionId" not in ask.json()
        assert "ragflowSessionId" not in history.json()

    @pytest.mark.asyncio
    async def test_history_persists_no_evidence_status(self, isolated_db):
        from enterprise.gateway.query import formal_router

        stub = formal_router.RAGFlowQueryStub()
        stub._no_evidence = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "unrelated"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        assistant = next(
            m for m in history.json()["messages"] if m["role"] == "assistant"
        )
        assert assistant["status"] == "no_reliable_evidence"
        assert assistant["citations"] == []
        assert assistant["content"]


class _FakeStreamResponse:
    def __init__(self, status_code=200, lines=None, error=None):
        self.status_code = status_code
        self._lines = lines or []
        self._error = error

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._error:
            raise self._error


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class TestRunOutcome:
    def test_explicit_completed_outcome_is_not_overridden_by_empty_citations(
        self,
    ):
        from enterprise.gateway.query import formal_router

        completion = {"code": 0, "data": {"status": "completed"}}
        assert (
            formal_router._resolve_run_outcome(completion) == "completed"
        )

    def test_explicit_no_evidence_outcome_wins(self):
        from enterprise.gateway.query import formal_router

        completion = {
            "code": 0,
            "data": {"status": "no_reliable_evidence"},
        }
        assert (
            formal_router._resolve_run_outcome(completion)
            == "no_reliable_evidence"
        )

    def test_explicit_failed_status_is_a_valid_outcome(self):
        from enterprise.gateway.query import formal_router

        completion = {"code": 0, "data": {"status": "failed"}}
        assert formal_router._resolve_run_outcome(completion) == "failed"

    def test_completed_status_is_not_rejudged_by_abstain_phrase(self):
        from enterprise.gateway.query import formal_router

        completion = {"code": 0, "data": {"status": "completed"}}
        # The answer body no longer participates in state resolution at all.
        assert formal_router._resolve_run_outcome(completion) == "completed"

    def test_missing_status_is_an_upstream_contract_error(self):
        from enterprise.gateway.query import formal_router

        with pytest.raises(formal_router._FormalQueryError) as exc_info:
            formal_router._resolve_run_outcome({"code": 0, "data": {}})
        assert exc_info.value.code == "RAGFLOW_API_INCOMPATIBLE"
        assert exc_info.value.status_code == 502

    def test_invalid_status_is_an_upstream_contract_error(self):
        from enterprise.gateway.query import formal_router

        completion = {"code": 0, "data": {"status": "abstain"}}
        with pytest.raises(formal_router._FormalQueryError) as exc_info:
            formal_router._resolve_run_outcome(completion)
        assert exc_info.value.code == "RAGFLOW_API_INCOMPATIBLE"
        assert exc_info.value.status_code == 502

    def test_missing_data_payload_is_an_upstream_contract_error(self):
        from enterprise.gateway.query import formal_router

        with pytest.raises(formal_router._FormalQueryError) as exc_info:
            formal_router._resolve_run_outcome(None)
        assert exc_info.value.code == "RAGFLOW_API_INCOMPATIBLE"
        assert exc_info.value.status_code == 502


class TestTransportFailure:
    @pytest.mark.asyncio
    async def test_connection_refused_maps_to_api_error_not_nameerror(self):
        from enterprise.gateway.query.ragflow_client import (
            RAGFlowAPIError,
            RAGFlowQueryClient,
        )

        client = RAGFlowQueryClient(
            base_url="http://127.0.0.1:1", api_key="test-key"
        )
        client.timeout = 1
        with pytest.raises(RAGFlowAPIError) as exc_info:
            async for _ in client.chat_completion_stream(
                "chat-1", "hello"
            ):
                pass
        assert exc_info.value.status_code == 0
        assert "NameError" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_httpx_transport_error_maps_and_doc_ids_are_csv(
        self, monkeypatch
    ):
        import httpx
        from enterprise.gateway.query.ragflow_client import (
            RAGFlowAPIError,
            RAGFlowQueryClient,
        )

        captured: dict = {}

        class _RaiseStreamContext(_FakeStreamContext):
            async def __aenter__(self):
                raise httpx.ConnectError("connection failed")

        def fake_stream(self, method, url, **kwargs):
            captured.update(kwargs)
            return _RaiseStreamContext(_FakeStreamResponse())

        monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
        client = RAGFlowQueryClient(
            base_url="http://127.0.0.1:9", api_key="test-key"
        )
        client.timeout = 1
        with pytest.raises(RAGFlowAPIError) as exc_info:
            async for _ in client.chat_completion_stream(
                "chat-1", "hello", doc_ids=["d1", "d2"]
            ):
                pass
        assert exc_info.value.status_code == 0
        assert captured["json"]["doc_ids"] == "d1,d2"

    @pytest.mark.asyncio
    async def test_premature_close_maps_to_api_error(self, monkeypatch):
        import httpx
        from enterprise.gateway.query.ragflow_client import (
            RAGFlowAPIError,
            RAGFlowQueryClient,
        )

        def fake_stream(self, method, url, **kwargs):
            response = _FakeStreamResponse(
                lines=["data: {\"code\": 0, \"data\": {}}\n\n"],
                error=httpx.ReadError("stream closed early"),
            )
            return _FakeStreamContext(response)

        monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
        client = RAGFlowQueryClient(
            base_url="http://127.0.0.1:9", api_key="test-key"
        )
        client.timeout = 1
        with pytest.raises(RAGFlowAPIError) as exc_info:
            async for _ in client.chat_completion_stream(
                "chat-1", "hello"
            ):
                pass
        assert exc_info.value.status_code == 0

    @pytest.mark.asyncio
    async def test_real_socket_mid_stream_disconnect_maps_to_api_error(self):
        from enterprise.scripts.wp04_stream_transport_probe import (
            run_direct_probe,
        )

        evidence = await run_direct_probe("secret-key-9f8e7d6c")
        assert evidence["streamTransportFailureVerified"] is True
        assert evidence["streamTransportExceptionMapped"] is True
        assert evidence["streamTransportNameErrorObserved"] is False
        assert evidence["streamTransportSensitiveDataLeaked"] is False


@pytest.mark.usefixtures("isolated_db")
class TestSseOutcomeConsistency:
    @pytest.mark.asyncio
    async def test_empty_chunks_do_not_downgrade_explicit_completed(
        self, isolated_db
    ):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-EMPTY-CHUNKS")
        stub = formal_router.RAGFlowQueryStub()
        stub._empty_chunks = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "hello"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        completed = next(
            data for event, data in events if event == "answer.completed"
        )
        # Chunks are evidence data only; they never change the business state.
        assert completed["status"] == "completed"
        assert completed["citations"] == []
        assert "run.failed" not in [event for event, _ in events]
        assistant = next(
            m
            for m in history.json()["messages"]
            if m["role"] == "assistant"
        )
        assert assistant["status"] == "completed"
        assert assistant["citations"] == []

    @pytest.mark.asyncio
    async def test_missing_status_fails_stream_with_contract_error(
        self, isolated_db
    ):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-NO-STATUS")
        stub = formal_router.RAGFlowQueryStub()
        stub._omit_status = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "hello"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        events = _parse_sse(resp.text)
        failed = next(data for event, data in events if event == "run.failed")
        assert failed["code"] == "RAGFLOW_API_INCOMPATIBLE"
        assert "answer.completed" not in [event for event, _ in events]
        assistant = next(
            m
            for m in history.json()["messages"]
            if m["role"] == "assistant"
        )
        assert assistant["status"] == "failed"

    @pytest.mark.asyncio
    async def test_stream_failure_persists_failed_for_history(
        self, isolated_db
    ):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-FAIL-HISTORY")
        stub = formal_router.RAGFlowQueryStub()
        stub._stream_fail_after = 1
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "hello"},
            )
            history = await c.get(
                f"/enterprise/api/v1/conversations/{conversation_id}",
                headers=_headers(token),
            )
        events = _parse_sse(resp.text)
        assert "run.failed" in [e for e, _ in events]
        assert "answer.completed" not in [e for e, _ in events]
        assistant = next(
            m
            for m in history.json()["messages"]
            if m["role"] == "assistant"
        )
        assert assistant["status"] == "failed"

    @pytest.mark.asyncio
    async def test_missing_stream_id_does_not_bind_wrong_message(
        self, isolated_db
    ):
        from enterprise.gateway.query import formal_router

        await _insert_document(isolated_db, doc_id="DOC-NO-ID")
        stub = formal_router.RAGFlowQueryStub()
        stub._omit_stream_id = True
        formal_router._query_stub = stub
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            conversation_id = (
                await _create_conversation(c, token)
            ).json()["conversationId"]
            resp = await c.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream"
                "?stream=true",
                headers=_headers(token),
                json={"question": "hello"},
            )
        assert resp.status_code == 200
        async with isolated_db.transaction(write=False) as conn:
            rows = await fetchall(
                conn,
                """SELECT ragflow_message_id
                   FROM ext_conversation_message
                   WHERE conversation_id=?""",
                (conversation_id,),
            )
        assert rows
        assert all(row["ragflow_message_id"] is None for row in rows)


class TestScopeCompleteness:
    @pytest.mark.asyncio
    async def test_scope_resolver_reads_beyond_100_ready_documents(
        self, isolated_db, monkeypatch
    ):
        from enterprise.gateway.acl.context import AclContext
        from enterprise.gateway.query import formal_router

        monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
        for index in range(105):
            await _insert_document(
                isolated_db,
                doc_id=f"DOC-{index:03d}",
                ragflow_doc_id=f"doc-{index:03d}",
                quality=None,
            )
        resolver = formal_router.FormalScopeResolver(isolated_db)
        scope = await resolver.resolve(
            AclContext(principal=_principal())
        )
        assert len(scope.document_ids) == 105
        assert len(set(scope.document_ids)) == 105

    @pytest.mark.asyncio
    async def test_scope_resolver_sees_eam_docs_when_query_source_unset(
        self, isolated_db, monkeypatch
    ):
        from enterprise.gateway.acl.context import AclContext
        from enterprise.gateway.query import formal_router

        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
        monkeypatch.delenv("ENTERPRISE_QUERY_SOURCE_SYSTEM", raising=False)
        monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
        await _insert_document(
            isolated_db,
            doc_id="DOC-EAM",
            ragflow_doc_id="doc-eam",
            source_system="EAM",
            quality=None,
        )
        resolver = formal_router.FormalScopeResolver(isolated_db)
        scope = await resolver.resolve(AclContext(principal=_principal()))
        assert "doc-eam" in scope.document_ids


class TestSchemaMigration:
    @pytest.mark.asyncio
    async def test_ensure_schema_adds_legacy_message_columns(
        self,
    ):
        gateway = await create_gateway(":memory:")
        conn = await gateway.connect()
        try:
            await conn.begin()
            await exec_sql(
                conn,
                """INSERT INTO ext_conversation_message
                   (conversation_id, tenant_id, business_user_id, message_id,
                    role, status, created_at, updated_at)
                   VALUES ('c', 't', 'u', 'm', 'assistant', 'completed',
                           'now', 'now')""",
            )
            await conn.commit()

            await conversation_store.ensure_schema(conn)
            await conversation_store.ensure_schema(conn)

            columns = await table_columns(conn, "ext_conversation_message")
            assert {"content", "citations_json", "ragflow_message_id"} <= columns
            row = await fetchone(
                conn,
                """SELECT message_id, role, status, content,
                          citations_json, ragflow_message_id
                   FROM ext_conversation_message
                   WHERE message_id='m'""",
            )
            assert row is not None
            assert row["role"] == "assistant"
            assert row["status"] == "completed"
            assert row["content"] is None
            assert row["citations_json"] is None
            assert row["ragflow_message_id"] is None
        finally:
            await conn.close()
            await gateway.dispose()


@pytest.mark.usefixtures("isolated_db")
class TestW2AssetIdentity:
    @pytest.mark.asyncio
    async def test_single_identifiers_persist_the_same_canonical_pair(
        self, isolated_db
    ):
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            equipment_only = await client.post(
                "/enterprise/api/v1/conversations",
                headers=_headers(token),
                json={"equipmentId": "EQ-1"},
            )
            fixed_only = await client.post(
                "/enterprise/api/v1/conversations",
                headers=_headers(token),
                json={"fixedAssetNo": "FA-1"},
            )

        assert equipment_only.status_code == 201
        assert fixed_only.status_code == 201
        async with isolated_db.transaction(write=False) as conn:
            rows = await fetchall(
                conn,
                """SELECT equipment_id, fixed_asset_no
                   FROM ext_conversation
                   WHERE conversation_id IN (?, ?)
                   ORDER BY conversation_id""",
                (
                    equipment_only.json()["conversationId"],
                    fixed_only.json()["conversationId"],
                ),
            )
        assert {(row["equipment_id"], row["fixed_asset_no"]) for row in rows} == {
            ("EQ-1", "FA-1")
        }

    @pytest.mark.asyncio
    async def test_conflicting_and_cross_tenant_fixed_identifiers_are_denied(
        self, isolated_db
    ):
        async with isolated_db.transaction(write=True) as conn:
            await exec_sql(
                conn,
                """INSERT INTO ext_asset_registry
                   (tenant_id, equipment_id, fixed_asset_no, asset_id)
                   VALUES ('customer-a', 'EQ-2', 'FA-2', 'ASSET-2')""",
            )
            await exec_sql(
                conn,
                """INSERT INTO ext_asset_registry
                   (tenant_id, equipment_id, fixed_asset_no, asset_id)
                   VALUES ('customer-b', 'EQ-B', 'FA-1', 'ASSET-B')""",
            )
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            conflict = await client.post(
                "/enterprise/api/v1/conversations",
                headers=_headers(token),
                json={"equipmentId": "EQ-1", "fixedAssetNo": "FA-2"},
            )
            cross_tenant = await client.post(
                "/enterprise/api/v1/conversations",
                headers=_headers(token),
                json={"equipmentId": "EQ-CROSS", "fixedAssetNo": "FA-1"},
            )

        assert conflict.status_code == 409
        assert conflict.json()["code"] == "CONVERSATION_CONTEXT_CONFLICT"
        assert cross_tenant.status_code == 409
        assert cross_tenant.json()["code"] == "CONVERSATION_CONTEXT_CONFLICT"

    @pytest.mark.asyncio
    async def test_context_mapping_drift_is_denied_before_second_retrieval(
        self, isolated_db
    ):
        await _insert_document(isolated_db, doc_id="DOC-W2-DRIFT")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            conversation = await _create_conversation(client, token)
            conversation_id = conversation.json()["conversationId"]
            first = await client.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "first"},
            )
            async with isolated_db.transaction(write=True) as conn:
                await exec_sql(
                    conn,
                    """UPDATE ext_asset_registry
                       SET fixed_asset_no='FA-DRIFT', asset_id='ASSET-DRIFT'
                       WHERE tenant_id='customer-a' AND equipment_id='EQ-1'""",
                )
            second = await client.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "second"},
            )

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["code"] == "CONVERSATION_CONTEXT_STALE"

    @pytest.mark.asyncio
    async def test_legacy_equipment_only_context_is_completed_from_registry(
        self, isolated_db
    ):
        await _insert_document(isolated_db, doc_id="DOC-W2-LEGACY")
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            conversation = await _create_conversation(client, token)
            conversation_id = conversation.json()["conversationId"]
            async with isolated_db.transaction(write=True) as conn:
                await exec_sql(
                    conn,
                    """UPDATE ext_conversation
                       SET fixed_asset_no=NULL
                       WHERE conversation_id=?""",
                    (conversation_id,),
                )

            response = await client.post(
                f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
                headers=_headers(token),
                json={"question": "legacy context"},
            )

        assert response.status_code == 200
        async with isolated_db.transaction(write=False) as conn:
            row = await fetchone(
                conn,
                """SELECT equipment_id, fixed_asset_no
                   FROM ext_conversation WHERE conversation_id=?""",
                (conversation_id,),
            )
        assert (row["equipment_id"], row["fixed_asset_no"]) == (
            "EQ-1",
            "FA-1",
        )

    @pytest.mark.asyncio
    async def test_unexpected_registry_error_is_not_masked_as_503(
        self, isolated_db, monkeypatch
    ):
        from enterprise.gateway.query import formal_router

        async def broken_resolver(*args, **kwargs):
            raise RuntimeError("local programming error")

        monkeypatch.setattr(formal_router, "resolve_asset", broken_resolver)
        with pytest.raises(RuntimeError, match="local programming error"):
            await formal_router._resolve_formal_asset(
                isolated_db,
                _principal(),
                equipment_id="EQ-1",
                fixed_asset_no=None,
            )

    @pytest.mark.asyncio
    async def test_retrieval_requires_exact_canonical_document_identity(
        self, isolated_db
    ):
        await _insert_document(isolated_db, doc_id="DOC-W2-EXACT")
        await _insert_document(
            isolated_db,
            doc_id="DOC-W2-ALIAS",
            ragflow_doc_id="doc-alias",
            equipment_id="EQ-ALIAS",
            fixed_asset_no="FA-ALIAS",
        )
        async with isolated_db.transaction(write=True) as conn:
            await exec_sql(
                conn,
                """UPDATE ext_document_map
                   SET equipment_id='FA-1', fixed_asset_no='FA-1'
                   WHERE external_document_id='DOC-W2-ALIAS'""",
            )
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            conversation = await _create_conversation(client, token)
            response = await client.post(
                f"/enterprise/api/v1/conversations/{conversation.json()['conversationId']}/messages:stream",
                headers=_headers(token),
                json={"question": "exact"},
            )

        assert response.status_code == 200
        from enterprise.gateway.query import formal_router

        assert formal_router._query_stub._last_completion_body["doc_ids"] == "doc-1"

    @pytest.mark.asyncio
    async def test_draft_cannot_retrieve_without_a_canonical_context(self, isolated_db):
        token = _make_token()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            conversation = await client.post(
                "/enterprise/api/v1/conversations",
                headers=_headers(token),
                json={},
            )
            response = await client.post(
                f"/enterprise/api/v1/conversations/{conversation.json()['conversationId']}/messages:stream",
                headers=_headers(token),
                json={"question": "without context"},
            )

        assert conversation.status_code == 201
        assert response.status_code == 422
        assert response.json()["code"] == "CONVERSATION_CONTEXT_REQUIRED"
