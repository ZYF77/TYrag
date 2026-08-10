"""P0 v2 external document contract tests, isolated from RAGFlow core."""
import asyncio
import hashlib

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.sync import v2_router
from enterprise.gateway.sync.models import (
    get_mapping,
    update_mapping_status,
    update_parser_application,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import SyncService


CONTENT_SHA = hashlib.sha256(b"test pdf content").hexdigest()


def _payload(
    *,
    event_id: str = "evt-v2-001",
    document_id: str = "DOC-V2-001",
    source_version_id: str = "v1",
    sha256: str = CONTENT_SHA,
) -> dict:
    return {
        "eventId": event_id,
        "eventType": "upsert",
        "tenantId": "tenant-a",
        "sourceSystem": "EAM",
        "externalDocumentId": document_id,
        "sourceVersionId": source_version_id,
        "sha256": sha256,
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": "equipment", "objectKey": "manual.pdf"},
        "metadata": {
            "schema_version": 1,
            "tenant_id": "tenant-a",
            "external_document_id": document_id,
            "source_system": "EAM",
            "equipment_id": "EQ-001",
            "fixed_asset_no": "FA-001",
            "document_type": "manual",
            "document_version": source_version_id,
            "department_id": "maintenance",
            "security_level": 2,
            "business_status": "active",
        },
    }


def test_asset_registry_outage_is_retryable():
    body = v2_router._error(
        503, "ASSET_REGISTRY_UNAVAILABLE", "request-1"
    ).body
    assert b'"retryable":true' in body


@pytest.fixture
def v2_app(isolated_gateway_db):
    db, _ = isolated_gateway_db
    application = FastAPI()
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    application.dependency_overrides[require_service_principal] = lambda: (
        ServicePrincipal(source_system="service")
    )
    return application


@pytest.mark.asyncio
async def test_first_ingestion_returns_external_only_response(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v2/documents", json=_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["externalDocumentId"] == "DOC-V2-001"
    assert body["sourceVersionId"] == "v1"
    assert body["deduplicated"] is False
    assert set(body) == {
        "operationId",
        "externalDocumentId",
        "sourceVersionId",
        "status",
        "stage",
        "deduplicated",
        "businessStatus",
        "currentVersion",
        "eventStatus",
        "updatedAt",
    }
    assert not any("ragflow" in key.lower() for key in body)


@pytest.mark.asyncio
async def test_same_event_and_normalized_payload_replays_result(v2_app):
    payload = _payload()
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v2/documents", json=payload)
        replay = await client.post(
            "/enterprise/api/v2/documents",
            json={
                **{key: payload[key] for key in reversed(payload)},
                "batchId": "retry-batch-is-not-part-of-normalized-payload",
            },
        )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["deduplicated"] is True
    assert replay.json()["externalDocumentId"] == first.json()["externalDocumentId"]


@pytest.mark.asyncio
async def test_same_event_with_different_payload_is_conflict(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v2/documents", json=_payload())
        changed = _payload()
        changed["fileName"] = "different.pdf"
        conflict = await client.post("/enterprise/api/v2/documents", json=changed)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "EVENT_ID_CONFLICT"


@pytest.mark.asyncio
async def test_same_document_version_and_sha_deduplicates_new_event(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v2/documents", json=_payload())
        duplicate = await client.post(
            "/enterprise/api/v2/documents", json=_payload(event_id="evt-v2-002")
        )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True


@pytest.mark.asyncio
async def test_concurrent_same_business_key_creates_one_outbox_event(
    v2_app, isolated_gateway_db
):
    db, _ = isolated_gateway_db
    payloads = [_payload(event_id=f"evt-concurrent-{index}") for index in range(2)]
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post("/enterprise/api/v2/documents", json=payload)
                for payload in payloads
            )
        )

    assert [response.status_code for response in responses] == [202, 202]
    assert sorted(response.json()["deduplicated"] for response in responses) == [
        False,
        True,
    ]
    async with db.execute(
        "SELECT COUNT(*) AS count FROM ext_document_map"
    ) as cursor:
        assert (await cursor.fetchone())["count"] == 1
    async with db.execute(
        "SELECT COUNT(*) AS count FROM sync_outbox"
    ) as cursor:
        assert (await cursor.fetchone())["count"] == 1


@pytest.mark.asyncio
async def test_concurrent_different_business_keys_share_one_sqlite_transaction(
    v2_app, isolated_gateway_db
):
    db, _ = isolated_gateway_db
    payloads = [
        _payload(event_id=f"evt-other-{index}", document_id=f"DOC-{index}")
        for index in range(2)
    ]
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post("/enterprise/api/v2/documents", json=payload)
                for payload in payloads
            )
        )

    assert [response.status_code for response in responses] == [202, 202]
    async with db.execute(
        "SELECT COUNT(*) AS count FROM ext_document_map"
    ) as cursor:
        assert (await cursor.fetchone())["count"] == 2
    async with db.execute(
        "SELECT COUNT(*) AS count FROM sync_outbox"
    ) as cursor:
        assert (await cursor.fetchone())["count"] == 2


@pytest.mark.asyncio
async def test_same_document_version_with_different_sha_is_conflict(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v2/documents", json=_payload())
        conflict = await client.post(
            "/enterprise/api/v2/documents",
            json=_payload(
                event_id="evt-v2-003", sha256=hashlib.sha256(b"changed").hexdigest()
            ),
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "DOCUMENT_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_status_requires_exact_tenant_and_source_scope(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        await client.post("/enterprise/api/v2/documents", json=_payload())
        missing_source = await client.get(
            "/enterprise/api/v2/documents/DOC-V2-001/status",
            params={"tenantId": "tenant-a"},
        )
        wrong_tenant = await client.get(
            "/enterprise/api/v2/documents/DOC-V2-001/status",
            params={"tenantId": "tenant-b", "sourceSystem": "EAM"},
        )
        found = await client.get(
            "/enterprise/api/v2/documents/DOC-V2-001/status",
            params={
                "tenantId": "tenant-a",
                "sourceSystem": "EAM",
                "sourceVersionId": "v1",
            },
        )

    assert missing_source.status_code == 422
    assert wrong_tenant.status_code == 404
    assert found.status_code == 200
    assert not any("ragflow" in key.lower() for key in found.json())


@pytest.mark.asyncio
async def test_credential_binding_restricts_tenant_source_pair(
    v2_app, isolated_gateway_db
):
    db, _ = isolated_gateway_db
    v2_app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="EAM",
        credential_id="credential-1",
        key_id="key-1",
        allowed_bindings=frozenset({("tenant-a", "EAM")}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        accepted = await client.post(
            "/enterprise/api/v2/documents", json=_payload()
        )
        denied = await client.get(
            "/enterprise/api/v2/documents/DOC-V2-001/status",
            params={"tenantId": "tenant-b", "sourceSystem": "EAM"},
        )

    assert accepted.status_code == 202
    assert denied.status_code == 403
    assert denied.json()["code"] == "ACL_DENIED"
    assert await get_mapping(db, "tenant-a", "EAM", "DOC-V2-001", "v1")


@pytest.mark.asyncio
async def test_legacy_bearer_cannot_access_v2(
    isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_SYNC_SERVICE_TOKEN", "legacy-v1-token")
    application = FastAPI()
    application.include_router(v2_router.router)
    application.dependency_overrides[v2_router.get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/enterprise/api/v2/documents",
            headers={"Authorization": "Bearer legacy-v1-token"},
            json=_payload(event_id="evt-bearer-v2-denied"),
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_SIGNATURE_MISSING"


@pytest.mark.asyncio
async def test_metadata_is_strict_snake_case_and_matches_envelope(v2_app):
    camel_metadata = _payload(event_id="evt-v2-meta-1")
    camel_metadata["metadata"]["tenantId"] = camel_metadata["metadata"].pop(
        "tenant_id"
    )
    mismatched_source = _payload(event_id="evt-v2-meta-2")
    mismatched_source["metadata"]["source_system"] = "OTHER"
    mismatched_tenant = _payload(event_id="evt-v2-meta-tenant")
    mismatched_tenant["metadata"]["tenant_id"] = "tenant-b"
    extra_envelope = _payload(event_id="evt-v2-meta-3")
    extra_envelope["source_system"] = "EAM"
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        camel_response = await client.post(
            "/enterprise/api/v2/documents", json=camel_metadata
        )
        mismatch_response = await client.post(
            "/enterprise/api/v2/documents", json=mismatched_source
        )
        tenant_response = await client.post(
            "/enterprise/api/v2/documents", json=mismatched_tenant
        )
        extra_response = await client.post(
            "/enterprise/api/v2/documents", json=extra_envelope
        )

    assert camel_response.status_code == 422
    assert mismatch_response.status_code == 422
    assert mismatch_response.json()["code"] == "DOCUMENT_METADATA_INVALID"
    assert tenant_response.status_code == 422
    assert extra_response.status_code == 422


@pytest.mark.asyncio
async def test_canonical_equipment_aliases_are_persisted(v2_app, isolated_gateway_db):
    db, _ = isolated_gateway_db
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v2/documents", json=_payload())

    assert response.status_code == 202
    doc = await get_mapping(db, "tenant-a", "EAM", "DOC-V2-001", "v1")
    assert doc is not None
    assert doc.asset_id == "FA-001"
    assert doc.equipment_id == "EQ-001"
    assert doc.fixed_asset_no == "FA-001"


@pytest.mark.asyncio
async def test_lifecycle_routes_keep_scope_and_external_responses(v2_app):
    query = {"tenantId": "tenant-a", "sourceSystem": "EAM"}
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        await client.post("/enterprise/api/v2/documents", json=_payload())
        disable = await client.post(
            "/enterprise/api/v2/documents/DOC-V2-001/disable", params=query
        )
        restore = await client.post(
            "/enterprise/api/v2/documents/DOC-V2-001/restore", params=query
        )
        delete = await client.delete(
            "/enterprise/api/v2/documents/DOC-V2-001", params=query
        )

    assert disable.status_code == 202
    assert disable.json() == {
        "externalDocumentId": "DOC-V2-001",
        "status": "accepted",
    }
    assert restore.status_code == 202
    assert delete.status_code == 202
    assert delete.json() == {
        "externalDocumentId": "DOC-V2-001",
        "status": "accepted",
    }
    assert all(
        not any("ragflow" in key.lower() for key in response.json())
        for response in (disable, restore, delete)
    )


@pytest.mark.asyncio
async def test_reindex_has_independent_idempotent_command(
    v2_app, isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    client_stub = RAGFlowDocumentStub()
    client_stub._documents["internal-document"] = {
        "data": [{
            "id": "internal-document",
            "dataset_id": "internal-dataset",
            "run": "DONE",
            "chunk_method": "naive",
            "parser_config": {"layout_recognize": "DeepDOC"},
        }]
    }
    monkeypatch.setattr(
        v2_router,
        "_sync_service",
        lambda connection: SyncService(connection, SourceStub(b"test pdf content"), client_stub),
    )
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        await client.post("/enterprise/api/v2/documents", json=_payload())
        doc = await get_mapping(db, "tenant-a", "EAM", "DOC-V2-001", "v1")
        assert doc is not None
        doc.ragflow_dataset_id = "internal-dataset"
        doc.ragflow_document_id = "internal-document"
        await update_mapping_status(
            db,
            doc,
            "ready",
            pipeline_status="DONE",
            event_status="completed",
        )
        await update_parser_application(db, doc, status="executed")
        command = _payload(event_id="evt-reindex-1")
        command["eventType"] = "reindex"
        first = await client.post(
            "/enterprise/api/v2/documents",
            json=command,
        )
        replay = await client.post(
            "/enterprise/api/v2/documents",
            json=command,
        )

    assert first.status_code == 202
    assert first.json()["status"] == "ready"
    assert first.json()["operationId"] == "evt-reindex-1"
    assert replay.status_code == 202
    assert replay.json()["deduplicated"] is True
    assert replay.json()["operationId"] == "evt-reindex-1"
    assert not any("ragflow" in key.lower() for key in first.json())


@pytest.mark.asyncio
async def test_sync_status_uses_scoped_cursor_page(v2_app):
    async with AsyncClient(
        transport=ASGITransport(app=v2_app), base_url="http://test"
    ) as client:
        for index in range(3):
            response = await client.post(
                "/enterprise/api/v2/documents",
                json=_payload(
                    event_id=f"evt-page-{index}",
                    document_id=f"DOC-PAGE-{index}",
                ),
            )
            assert response.status_code == 202
        first = await client.get(
            "/enterprise/api/v2/documents/sync-status",
            params={"tenantId": "tenant-a", "sourceSystem": "EAM", "limit": 2},
        )
        second = await client.get(
            "/enterprise/api/v2/documents/sync-status",
            params={
                "tenantId": "tenant-a",
                "sourceSystem": "EAM",
                "limit": 2,
                "cursor": first.json()["nextCursor"],
            },
        )

    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["hasMore"] is True
    assert first.json()["nextCursor"]
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["hasMore"] is False
