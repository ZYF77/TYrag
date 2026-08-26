"""WP-02B tests: S3 adapter, state machine, outbox, retry, and lifecycle."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["ENTERPRISE_TEST_MODE"] = "1"
os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "false"

from enterprise.gateway.app import app
from enterprise.gateway.sync.models import (
    OutboxEvent,
    claim_outbox,
    enqueue_outbox,
    get_mapping_by_event_id,
    get_outbox_by_event_id,
    init_db,
    list_mappings,
    mark_outbox_failed,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import (
    S3SourceAdapter,
    SourceHashMismatch,
    SourceStub,
    SourceTooLarge,
)
from enterprise.gateway.sync.state_machine import (
    is_terminal_document_status,
    transition_allowed,
    validate_transition,
)
from enterprise.gateway.sync.sync_service import (
    RetryableDocumentSyncError,
    SyncService,
    TerminalDocumentSyncError,
)
from enterprise.gateway.sync.worker import OutboxWorker

VALID_METADATA = {
    "schema_version": 1,
    "tenant_id": "tenant-1",
    "external_document_id": "DOC-1",
    "source_system": "EAM",
    "equipment_id": "EQ-001",
    "document_type": "manual",
    "document_version": "v1",
    "department_id": "dept-eng",
    "security_level": 3,
    "business_status": "active",
}


def payload_for(
    content: bytes,
    event_id: str = "evt-1",
    doc_id: str = "DOC-1",
    version: str = "v1",
) -> dict:
    return {
        "eventId": event_id,
        "eventType": "upsert",
        "sourceSystem": "EAM",
        "externalDocumentId": doc_id,
        "sourceVersionId": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": f"{doc_id}.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": "docs", "objectKey": f"{doc_id}/{version}.pdf"},
        "metadata": {**VALID_METADATA, "tenant_id": "tenant-1"},
        "batchId": None,
    }


def make_event(content: bytes, **kwargs) -> OutboxEvent:
    payload = payload_for(content, **kwargs)
    return OutboxEvent(
        event_id=payload["eventId"],
        event_type=payload["eventType"],
        tenant_id="tenant-1",
        source_system=payload["sourceSystem"],
        external_document_id=payload["externalDocumentId"],
        source_version_id=payload["sourceVersionId"],
        batch_id=payload.get("batchId"),
        payload=json.dumps(payload),
        max_attempts=5,
    )


class TestStateMachine:
    def test_document_progression(self):
        assert transition_allowed("received", "validated")
        assert transition_allowed("registered", "parsing")
        assert transition_allowed("parsing", "ready")

    def test_terminal_status_not_downgraded(self):
        assert is_terminal_document_status("ready")
        assert not transition_allowed("ready", "parsing")
        assert transition_allowed("ready", "superseded")

    def test_invalid_transition_raises(self):
        with pytest.raises(ValueError):
            validate_transition("ready", "parsing")


class TestOutbox:
    @pytest.mark.asyncio
    async def test_enqueue_is_idempotent(self):
        db = await init_db(":memory:")
        event = make_event(b"a")
        first = await enqueue_outbox(db, event)
        second = await enqueue_outbox(db, make_event(b"a"))
        assert first.id == second.id
        assert (await get_outbox_by_event_id(db, "evt-1")).status == "pending"
        await db.close()

    @pytest.mark.asyncio
    async def test_claim_increments_attempts(self):
        db = await init_db(":memory:")
        await enqueue_outbox(db, make_event(b"a"))
        claimed = await claim_outbox(db, "worker-1")
        assert len(claimed) == 1
        assert claimed[0].attempts == 1
        assert claimed[0].status == "processing"
        await db.close()

    @pytest.mark.asyncio
    async def test_max_attempts_dead_letter(self):
        db = await init_db(":memory:")
        event = make_event(b"a")
        event.max_attempts = 1
        await enqueue_outbox(db, event)
        claimed = await claim_outbox(db, "worker-1")
        await mark_outbox_failed(db, claimed[0], "RAGFLOW_UNAVAILABLE", "boom")
        assert (await get_outbox_by_event_id(db, "evt-1")).status == "dead"
        await db.close()


class TestS3SourceAdapter:
    def _adapter(self, content: bytes, content_type: str = "application/pdf"):
        class FakeBody:
            def __init__(self, data):
                self._data = data

            def read(self, size: int = -1):
                if size >= 0:
                    return self._data[:size]
                return self._data

        class FakeClient:
            def get_object(self, Bucket, Key):
                return {"Body": FakeBody(content), "ContentType": content_type}

        adapter = S3SourceAdapter(max_size_bytes=1024 * 1024)
        adapter._client = lambda: FakeClient()
        return adapter

    @pytest.mark.asyncio
    async def test_fetch_and_hash(self):
        content = b"minio object bytes"
        adapter = self._adapter(content)
        source = await adapter.fetch(
            "bucket", "folder/manual.pdf", hashlib.sha256(content).hexdigest()
        )
        assert source.content == content
        assert source.file_name == "manual.pdf"
        assert source.size == len(content)

    @pytest.mark.asyncio
    async def test_hash_mismatch(self):
        adapter = self._adapter(b"a")
        with pytest.raises(SourceHashMismatch):
            await adapter.fetch("bucket", "key", "b" * 64)

    @pytest.mark.asyncio
    async def test_size_limit(self):
        adapter = S3SourceAdapter(max_size_bytes=3)

        class FakeBody:
            def read(self, size: int = -1):
                return b"1234"

        class FakeClient:
            def get_object(self, Bucket, Key):
                return {"Body": FakeBody(), "ContentType": "application/pdf"}

        adapter._client = lambda: FakeClient()
        with pytest.raises(SourceTooLarge):
            await adapter.fetch("bucket", "key")

    def test_path_style_env_is_honored(self):
        import os
        previous = os.environ.get("S3_PATH_STYLE")
        os.environ["S3_PATH_STYLE"] = "false"
        try:
            assert S3SourceAdapter().path_style is False
        finally:
            if previous is None:
                os.environ.pop("S3_PATH_STYLE", None)
            else:
                os.environ["S3_PATH_STYLE"] = previous


class TestSyncService:
    @pytest.mark.asyncio
    async def test_ready_and_ten_replays(self):
        db = await init_db(":memory:")
        content = b"manual v1"
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        doc, dedup = await service.process_event(event)
        assert doc.sync_status == "ready"
        assert doc.current_version == 0
        assert doc.parser_application_status == "executed"
        assert await service.promote_quality_passed_version(doc, "passed")
        assert doc.current_version == 1
        assert not dedup
        for _ in range(10):
            _, is_dup = await service.process_event(event)
            assert is_dup
        assert len(await list_mappings(db)) == 1
        assert len(client._documents) == 1
        await db.close()

    @pytest.mark.asyncio
    async def test_upload_triggers_ragflow_parse(self):
        db = await init_db(":memory:")
        content = b"manual to parse"
        client = RAGFlowDocumentStub()
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        doc, _ = await service.process_event(event)
        assert doc.sync_status == "parsing"
        assert len(client._parse_calls) == 1
        dataset_id, document_ids = client._parse_calls[0]
        assert document_ids == [doc.ragflow_document_id]
        assert doc.ragflow_dataset_id == dataset_id
        await db.close()

    @pytest.mark.asyncio
    async def test_new_version_waits_for_quality_before_superseding_old(self):
        db = await init_db(":memory:")
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(db, SourceStub(b"v1"), client)
        old_event = make_event(b"v1", event_id="evt-v1", version="v1")
        old_doc, _ = await service.process_event(old_event)
        assert await service.promote_quality_passed_version(old_doc, "passed")
        service = SyncService(db, SourceStub(b"v2"), client)
        new_event = make_event(b"v2", event_id="evt-v2", version="v2")
        new_doc, _ = await service.process_event(new_event)
        assert new_doc.sync_status == "ready"
        assert new_doc.current_version == 0
        versions = await list_mappings(db)
        old = next(v for v in versions if v.source_version_id == "v1")
        assert old.sync_status == "ready"
        assert old.business_status == "active"
        assert old.current_version == 1

        assert await service.promote_quality_passed_version(new_doc, "passed")
        versions = await list_mappings(db)
        old = next(v for v in versions if v.source_version_id == "v1")
        new = next(v for v in versions if v.source_version_id == "v2")
        assert new.sync_status == "ready"
        assert new.business_status == "active"
        assert new.current_version == 1
        assert old.sync_status == "superseded"
        assert old.business_status == "superseded"
        assert old.current_version == 0
        assert client._status_updates and client._status_updates[-1][2] is False
        await db.close()

    @pytest.mark.asyncio
    async def test_hash_mismatch_is_terminal(self):
        db = await init_db(":memory:")
        content = b"manual"
        client = RAGFlowDocumentStub()
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        event.payload = json.dumps(
            {**json.loads(event.payload), "sha256": "b" * 64}
        )
        with pytest.raises(TerminalDocumentSyncError):
            await service.process_event(event)
        doc = await get_mapping_by_event_id(db, "evt-1")
        assert doc.sync_status == "failed"
        assert len(client._documents) == 0
        await db.close()

    @pytest.mark.asyncio
    async def test_worker_retries_without_duplicate(self):
        db = await init_db(":memory:")
        content = b"retry me"
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        client._fail_next = True
        service = SyncService(db, SourceStub(content), client)
        worker = OutboxWorker(service, "worker-retry")
        await enqueue_outbox(db, make_event(content))
        await worker.run_once()
        outbox = await get_outbox_by_event_id(db, "evt-1")
        assert outbox.status == "pending"
        assert outbox.attempts == 1
        assert outbox.next_retry_at
        client._fail_next = False
        await db.execute(
            "UPDATE sync_outbox SET next_retry_at=NULL WHERE event_id=?",
            ("evt-1",),
        )
        await db.commit()
        await worker.run_once()
        assert (await get_outbox_by_event_id(db, "evt-1")).status == "done"
        doc = await get_mapping_by_event_id(db, "evt-1")
        assert doc.sync_status == "ready"
        assert len(client._documents) == 1
        await db.close()

    @pytest.mark.asyncio
    async def test_worker_dead_letter_after_max_attempts(self, monkeypatch):
        db = await init_db(":memory:")
        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENABLED", "true")
        monkeypatch.setenv("ENTERPRISE_CALLBACK_HMAC_SECRET", "cb-secret")
        monkeypatch.setenv(
            "ENTERPRISE_CALLBACK_ENDPOINTS",
            json.dumps({"EAM": "https://eam.example/callback"}),
        )
        from enterprise.gateway import config as config_module
        from enterprise.gateway.callback_delivery import get_callback_delivery

        monkeypatch.setattr(config_module.config, "callback_enabled", True)
        monkeypatch.setattr(config_module.config, "callback_hmac_secret", "cb-secret")
        monkeypatch.setattr(config_module.config, "quality_worker_enabled", False)

        client = RAGFlowDocumentStub()
        client._fail_next = True
        service = SyncService(db, SourceStub(b"dead"), client)
        worker = OutboxWorker(service, "worker-dead")
        event = make_event(b"dead")
        event.max_attempts = 1
        await enqueue_outbox(db, event)
        await worker.run_once()
        assert (await get_outbox_by_event_id(db, "evt-1")).status == "dead"
        doc = await get_mapping_by_event_id(db, "evt-1")
        assert doc is not None
        assert doc.sync_status == "failed"
        assert doc.event_status == "failed"
        delivery = await get_callback_delivery(
            db,
            tenant_id="tenant-1",
            source_system="EAM",
            external_document_id="DOC-1",
            source_version_id="v1",
            terminal_status="failed",
        )
        assert delivery is not None
        assert delivery.state == "pending"
        await db.close()

    @pytest.mark.asyncio
    async def test_finalize_outbox_exhausted_skips_terminal_document(
        self, monkeypatch,
    ):
        db = await init_db(":memory:")
        monkeypatch.setenv("ENTERPRISE_CALLBACK_ENABLED", "true")
        monkeypatch.setenv("ENTERPRISE_CALLBACK_HMAC_SECRET", "cb-secret")
        monkeypatch.setenv(
            "ENTERPRISE_CALLBACK_ENDPOINTS",
            json.dumps({"EAM": "https://eam.example/callback"}),
        )
        from enterprise.gateway import config as config_module
        from enterprise.gateway.callback_delivery import get_callback_delivery

        monkeypatch.setattr(config_module.config, "callback_enabled", True)
        monkeypatch.setattr(config_module.config, "callback_hmac_secret", "cb-secret")

        content = b"already-ready"
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        doc, _ = await service.process_event(event)
        assert doc.sync_status == "ready"
        await service.finalize_outbox_exhausted(
            event, "INTERNAL_ERROR", "should not demote ready",
        )
        refreshed = await get_mapping_by_event_id(db, "evt-1")
        assert refreshed.sync_status == "ready"
        assert await get_callback_delivery(
            db,
            tenant_id="tenant-1",
            source_system="EAM",
            external_document_id="DOC-1",
            source_version_id="v1",
            terminal_status="failed",
        ) is None
        await db.close()

    @pytest.mark.asyncio
    async def test_metadata_failure_retries_without_duplicate(self):
        db = await init_db(":memory:")
        content = b"metadata retry"
        client = RAGFlowDocumentStub()
        client.run_status = "DONE"
        client._fail_metadata_next = True
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        with pytest.raises(RetryableDocumentSyncError):
            await service.process_event(event)
        doc = await get_mapping_by_event_id(db, "evt-1")
        assert doc.ragflow_document_id
        assert len(client._documents) == 1

        client._fail_metadata_next = False
        doc, _ = await service.process_event(event)
        assert doc.sync_status == "ready"
        assert len(client._documents) == 1
        await db.close()


class TestLifecycle:
    async def _ready_doc(self, db, client, content=b"lifecycle"):
        client.run_status = "DONE"
        service = SyncService(db, SourceStub(content), client)
        event = make_event(content)
        doc, _ = await service.process_event(event)
        assert await service.promote_quality_passed_version(doc, "passed")
        return service, doc

    @pytest.mark.asyncio
    async def test_disable_restore_delete(self):
        db = await init_db(":memory:")
        client = RAGFlowDocumentStub()
        service, doc = await self._ready_doc(db, client)

        versions = await service.disable_document(
            doc.tenant_id, doc.source_system, doc.external_document_id
        )
        assert versions[0].sync_status == "disabled"
        assert versions[0].business_status == "disabled"
        assert client._status_updates[-1][2] is False

        restored = await service.restore_document(
            doc.tenant_id, doc.source_system, doc.external_document_id
        )
        assert restored.sync_status == "ready"
        assert restored.business_status == "active"
        assert any(
            enabled for _, _, enabled in client._status_updates if enabled
        )

        deleted = await service.delete_document(
            doc.tenant_id, doc.source_system, doc.external_document_id
        )
        assert deleted[0].sync_status == "deleted"
        assert deleted[0].business_status == "deleted"

        restored_after_delete = await service.restore_document(
            doc.tenant_id, doc.source_system, doc.external_document_id
        )
        assert restored_after_delete.sync_status == "ready"
        assert restored_after_delete.business_status == "active"
        await db.close()


@pytest.mark.usefixtures("isolated_gateway_db")
class TestSyncStatusAPI:
    @pytest.mark.asyncio
    async def test_sync_status_list_and_disable(self):
        content = b"api content"
        payload = payload_for(content)
        payload["batchId"] = "batch-1"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/enterprise/api/v1/documents", json=payload,
            )
            assert resp.status_code == 202
            list_resp = await c.get(
                "/enterprise/api/v1/documents/sync-status",
                params={"tenant_id": "tenant-1"},
            )
            assert list_resp.status_code == 200
            items = list_resp.json()
            assert len(items) == 1
            assert items[0]["externalDocumentId"] == "DOC-1"
            batch_resp = await c.get(
                "/enterprise/api/v1/documents/sync-status",
                params={"tenant_id": "tenant-1", "batch_id": "batch-1"},
            )
            assert len(batch_resp.json()) == 1
            disable_resp = await c.post(
                "/enterprise/api/v1/documents/DOC-1/disable",
                params={"tenant_id": "tenant-1", "source_system": "EAM"},
            )
            assert disable_resp.status_code == 202
            status_resp = await c.get(
                "/enterprise/api/v1/documents/DOC-1/status",
                params={"tenant_id": "tenant-1"},
            )
            assert status_resp.json()["status"] == "disabled"
