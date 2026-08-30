"""Gateway must follow RAGFlow-side document deletion."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.app import app
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.db.dialect import exec_sql
from enterprise.gateway.db.testing import create_gateway
from enterprise.gateway.sync.models import (
    get_mapping,
    get_outbox_by_event_id,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import SyncService
from enterprise.gateway.sync.worker import StatusReconciler
from enterprise.tests.test_file_share_v3_status import _payload
from enterprise.tests.test_wp02b import make_event


@pytest.fixture
def v3_app(isolated_gateway_db):
    app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        yield app
    finally:
        app.dependency_overrides.pop(require_service_principal, None)


@pytest.mark.asyncio
async def test_refresh_status_marks_ready_deleted_when_ragflow_doc_missing():
    gateway = await create_gateway(":memory:")
    try:
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(gateway, SourceStub(b"manual"), client)

        doc, _ = await service.process_event(make_event(b"manual"))
        assert doc.sync_status == "ready"
        ragflow_id = doc.ragflow_document_id
        await client.delete_documents(doc.ragflow_dataset_id, [ragflow_id])

        updated = await service.refresh_status(doc)
        assert updated.sync_status == "deleted"
        assert updated.business_status == "deleted"
        assert not updated.ragflow_document_id
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_reconciler_marks_ready_mapping_when_dataset_no_longer_has_doc():
    gateway = await create_gateway(":memory:")
    try:
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(gateway, SourceStub(b"manual"), client)

        doc, _ = await service.process_event(make_event(b"manual"))
        await client.delete_documents(doc.ragflow_dataset_id, [doc.ragflow_document_id])

        marked = await StatusReconciler(service).run_once()
        async with gateway.transaction(write=False) as conn:
            current = await get_mapping(
                conn, doc.tenant_id, doc.source_system,
                doc.external_document_id, doc.source_version_id,
            )
        assert marked >= 1
        assert current.sync_status == "deleted"
        assert not current.ragflow_document_id
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_process_event_reregisters_after_ragflow_delete():
    gateway = await create_gateway(":memory:")
    try:
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(gateway, SourceStub(b"manual"), client)
        event = make_event(b"manual")

        first, _ = await service.process_event(event)
        old_id = first.ragflow_document_id
        await client.delete_documents(first.ragflow_dataset_id, [old_id])
        await service.refresh_status(first)

        second, _ = await service.process_event(event)
        assert second.sync_status == "ready"
        assert second.ragflow_document_id
        assert second.ragflow_document_id != old_id
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_v3_same_event_replay_reregisters_after_gateway_marks_deleted(
    v3_app, isolated_gateway_db,
):
    gateway, _db_path = isolated_gateway_db
    payload = _payload()
    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v3/documents", json=payload)
        assert first.status_code == 202
        assert first.json()["deduplicated"] is False

        async with gateway.transaction(write=True) as conn:
            existing = await get_mapping(conn, "tenant-a", "DEMO", "DOC-V3-001", "v1")
            await update_mapping_status(
                conn, existing, "ready", event_status="completed",
                business_status="active",
            )
            await exec_sql(
                conn,
                """UPDATE ext_document_map
                      SET ragflow_document_id=?, ragflow_dataset_id=?
                    WHERE id=?""",
                ("doc-gone", "ds-gone", existing.id),
            )

        service = SyncService(gateway, SourceStub(b"manual"), RAGFlowDocumentStub())
        async with gateway.transaction(write=False) as conn:
            existing = await get_mapping(conn, "tenant-a", "DEMO", "DOC-V3-001", "v1")
        await service.mark_ragflow_document_missing(existing)

        replay = await client.post("/enterprise/api/v3/documents", json=payload)

    assert replay.status_code == 202
    assert replay.json()["deduplicated"] is False
    async with gateway.transaction(write=False) as conn:
        outbox = await get_outbox_by_event_id(conn, payload["eventId"])
        mapping = await get_mapping(conn, "tenant-a", "DEMO", "DOC-V3-001", "v1")
    assert outbox is not None
    assert outbox.status == "pending"
    assert mapping.sync_status == "registered"
    assert not mapping.ragflow_document_id


@pytest.mark.asyncio
async def test_refresh_status_updates_ready_running_pipeline_when_ragflow_done():
    gateway = await create_gateway(":memory:")
    try:
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(gateway, SourceStub(b"manual"), client)

        doc, _ = await service.process_event(make_event(b"manual"))
        assert doc.sync_status == "ready"
        async with gateway.transaction(write=True) as conn:
            await exec_sql(
                conn,
                """UPDATE ext_document_map
                      SET pipeline_status='RUNNING',
                          parser_application_status='configured',
                          parser_executed_json=NULL
                    WHERE id=?""",
                (doc.id,),
            )
            stalled = await get_mapping(
                conn, doc.tenant_id, doc.source_system,
                doc.external_document_id, doc.source_version_id,
            )
        assert stalled.pipeline_status == "RUNNING"

        updated = await service.refresh_status(stalled)
        assert updated.sync_status == "ready"
        assert updated.pipeline_status == "DONE"
    finally:
        await gateway.dispose()


@pytest.mark.asyncio
async def test_reconciler_refreshes_ready_docs_still_running_in_ragflow():
    gateway = await create_gateway(":memory:")
    try:
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(gateway, SourceStub(b"manual"), client)

        doc, _ = await service.process_event(make_event(b"manual"))
        async with gateway.transaction(write=True) as conn:
            await exec_sql(
                conn,
                """UPDATE ext_document_map
                      SET pipeline_status='RUNNING',
                          parser_application_status='configured',
                          parser_executed_json=NULL
                    WHERE id=?""",
                (doc.id,),
            )

        await StatusReconciler(service).run_once()
        async with gateway.transaction(write=False) as conn:
            current = await get_mapping(
                conn, doc.tenant_id, doc.source_system,
                doc.external_document_id, doc.source_version_id,
            )
        assert current.sync_status == "ready"
        assert current.pipeline_status == "DONE"
    finally:
        await gateway.dispose()
