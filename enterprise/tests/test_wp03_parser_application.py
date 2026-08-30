from enterprise.gateway.db.ops import gw_read
import pytest
from types import SimpleNamespace

from enterprise.gateway.app import make_status_response
from enterprise.gateway.quality.routing import route_document
from enterprise.gateway.sync.models import get_mapping
from enterprise.gateway.db.testing import create_gateway
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentStub,
)
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import (
    RetryableDocumentSyncError,
    SyncService,
    TerminalDocumentSyncError,
)
from enterprise.tests.test_wp02b import make_event


def test_safe_parser_profiles_remain_available_for_admin_docs_only():
    """Routing helpers stay for Dataset/admin tooling; Gateway sync no longer applies them."""
    for name in ("scan.pdf", "table.pdf", "flowchart.pdf"):
        route = route_document(media_type="application/pdf", file_name=name)
        assert route["selected_parser_profile"] == "pdf_deepdoc_v1"
        assert route["chunk_method"] == "naive"
        assert route["parser_config"]["layout_recognize"] == "DeepDOC"
    assert route_document(
        media_type="image/png", file_name="photo.png",
    )["chunk_method"] == "picture"
    assert route_document(
        media_type="text/csv", file_name="data.csv",
    )["chunk_method"] == "table"


def test_client_profile_override_is_ignored_by_server_routing():
    route = route_document(
        media_type="application/pdf",
        file_name="manual.pdf",
        manual_profile="tabular_table_v1",
    )
    assert route["selected_parser_profile"] == "pdf_deepdoc_v1"
    assert route["parser_version"] == "1"
    assert route["client_override_ignored"] is True
    assert route["whether_manual_override"] is False


@pytest.mark.asyncio
async def test_sync_writes_enterprise_metadata_without_parser_override():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))

    assert doc.sync_status == "ready"
    assert doc.current_version == 0
    assert doc.equipment_id == "EQ-001"
    assert "patch" in client._operation_log
    assert client._operation_log.index("patch") < client._operation_log.index("parse")
    ragflow_doc = client._documents[doc.ragflow_document_id]["data"][0]
    assert ragflow_doc["meta_fields"]["enterprise_external_document_id"] == "DOC-1"
    assert ragflow_doc["meta_fields"]["equipment_id"] == "EQ-001"
    # Dataset/RAGFlow retains whatever parser settings already exist; Gateway
    # must not force a profile through the upload path.
    assert ragflow_doc["chunk_method"] == "naive"
    assert ragflow_doc["parser_config"] == {}
    await db.dispose()


@pytest.mark.asyncio
async def test_reindex_does_not_require_legacy_parser_evidence():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))
    await service.reindex_document("tenant-1", "EAM", "DOC-1", "v1")
    current = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    assert current.sync_status in {"ready", "queued", "parsing", "registered"}
    assert len(client._parse_calls) >= 2
    await db.dispose()


def test_status_response_reports_ragflow_owned_parser_application():
    doc = SimpleNamespace(
        external_document_id="DOC-1",
        source_version_id="v1",
        ragflow_dataset_id="dataset-1",
        ragflow_document_id="document-1",
        sync_status="ready",
        business_status="active",
        current_version=1,
        event_status="completed",
        updated_at="",
    )
    parser_application = make_status_response(doc)["parserApplication"]
    assert parser_application["state"] == "ragflow_owned"
    assert parser_application["readbackMatch"] is True
    assert parser_application["reasonCode"] is None
    assert parser_application["selectedProfile"] is None


class EmptyChunksStub(RAGFlowDocumentStub):
    async def list_chunks(
        self,
        dataset_id,
        document_id,
        page=1,
        page_size=30,
        request_id=None,
    ):
        del dataset_id, document_id, page, page_size, request_id
        return {"code": 0, "data": {"total": 0, "chunks": [], "doc": {}}}


class IncorrectDigestSource(SourceStub):
    async def fetch(self, bucket, object_key, expected_sha256=None):
        source = await super().fetch(bucket, object_key, expected_sha256)
        source.sha256 = "0" * 64
        return source


class MissingPostParseReadbackStub(RAGFlowDocumentStub):
    async def list_documents(
        self, dataset_id, document_id=None, page=1, page_size=100, request_id=None,
    ):
        if self._parse_calls and document_id:
            return []
        return await super().list_documents(
            dataset_id, document_id, page, page_size, request_id
        )


class RefreshFailureStub(RAGFlowDocumentStub):
    def __init__(self):
        super().__init__()
        self.fail_reads = False

    async def list_documents(
        self, dataset_id, document_id=None, page=1, page_size=100, request_id=None,
    ):
        if self.fail_reads:
            raise RAGFlowAPIError("RAGFlow unavailable", 503)
        return await super().list_documents(
            dataset_id, document_id, page, page_size, request_id
        )


class BatchStatusErrorStub(RAGFlowDocumentStub):
    async def batch_update_status(
        self, dataset_id, document_ids, enabled, request_id=None,
    ):
        return {
            "code": 0,
            "data": {document_id: {"error": "document unavailable"}
                     for document_id in document_ids},
        }


@pytest.mark.asyncio
async def test_done_with_empty_chunks_retries_parse_once():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = EmptyChunksStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))

    assert doc.sync_status == "queued"
    assert doc.parse_retry_count == 1
    assert doc.pipeline_status == "RUNNING"
    assert len(client._parse_calls) == 2
    await db.dispose()


@pytest.mark.asyncio
async def test_parser_config_mismatch_no_longer_blocks_ready():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))
    assert doc.sync_status == "ready"

    client._documents[doc.ragflow_document_id]["data"][0]["parser_config"] = {
        "layout_recognize": "Plain Text",
    }
    refreshed = await service.refresh_status(doc)
    assert refreshed.sync_status == "ready"
    await db.dispose()


@pytest.mark.asyncio
async def test_source_digest_is_verified_before_ragflow_registration():
    gateway = await create_gateway(":memory:")
    db = gateway
    content = b"manual"
    client = RAGFlowDocumentStub()
    service = SyncService(db, IncorrectDigestSource(content), client)

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.process_event(make_event(content))

    assert error.value.code == "DOCUMENT_HASH_MISMATCH"
    doc = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    assert doc.sync_status == "failed"
    assert doc.business_status == "review_required"
    assert doc.current_version == 0
    assert client._documents == {}
    await db.dispose()


@pytest.mark.asyncio
async def test_parse_failure_retries_once_then_can_fail():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RAGFlowDocumentStub()
    client.run_status = "FAIL"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))

    # First FAIL triggers one technical retry and leaves the document queued.
    assert doc.sync_status == "queued"
    assert doc.parse_retry_count == 1
    assert len(client._parse_calls) == 2

    # Simulate the retried parse finishing as FAIL again.
    client._documents[doc.ragflow_document_id]["data"][0]["run"] = "FAIL"
    refreshed = await service.refresh_status(doc)
    assert refreshed.sync_status == "failed"
    assert refreshed.business_status == "review_required"
    assert refreshed.current_version == 0
    assert await service.promote_quality_passed_version(refreshed, "passed") is False
    await db.dispose()


@pytest.mark.asyncio
async def test_missing_terminal_readback_is_retryable_and_not_ready():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = MissingPostParseReadbackStub()
    service = SyncService(db, SourceStub(b"manual"), client)

    with pytest.raises(RetryableDocumentSyncError) as error:
        await service.process_event(make_event(b"manual"))

    assert error.value.code == "RAGFLOW_UNAVAILABLE"
    doc = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    assert doc.sync_status == "retry_wait"
    assert doc.current_version == 0
    await db.dispose()


@pytest.mark.asyncio
async def test_status_refresh_failure_maps_to_stable_error():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RefreshFailureStub()
    client.run_status = "RUNNING"
    service = SyncService(db, SourceStub(b"manual"), client)
    doc, _ = await service.process_event(make_event(b"manual"))
    client.fail_reads = True

    with pytest.raises(RetryableDocumentSyncError) as error:
        await service.refresh_status(doc)

    assert error.value.code == "RAGFLOW_UNAVAILABLE"
    await db.dispose()


@pytest.mark.asyncio
async def test_lifecycle_does_not_commit_when_ragflow_reports_document_error():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = BatchStatusErrorStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)
    doc, _ = await service.process_event(make_event(b"manual"))

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.disable_document("tenant-1", "EAM", "DOC-1")

    assert error.value.code == "RAGFLOW_API_INCOMPATIBLE"
    current = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    assert current.sync_status == "ready"
    assert current.business_status == "active"
    assert doc.current_version == 0
    await db.dispose()


@pytest.mark.asyncio
async def test_only_quality_passed_latest_version_is_promoted():
    gateway = await create_gateway(":memory:")
    db = gateway
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"

    first_service = SyncService(db, SourceStub(b"v1"), client)
    first, _ = await first_service.process_event(
        make_event(b"v1", event_id="evt-v1", version="v1"),
    )
    assert await first_service.promote_quality_passed_version(first, "review_required") is False
    assert first.current_version == 0
    assert await first_service.promote_quality_passed_version(first, "passed") is True
    assert first.current_version == 1

    second_service = SyncService(db, SourceStub(b"v2"), client)
    second, _ = await second_service.process_event(
        make_event(b"v2", event_id="evt-v2", version="v2"),
    )
    first = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    assert first.current_version == 1
    assert second.current_version == 0

    assert await second_service.promote_quality_passed_version(second, "passed") is True
    first = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v1")
    second = await gw_read(db, get_mapping, "tenant-1", "EAM", "DOC-1", "v2")
    assert first.current_version == 0
    assert first.business_status == "superseded"
    assert second.current_version == 1
    assert any(enabled is False for _, _, enabled in client._status_updates)
    stale_attempt = await first_service.promote_quality_passed_version(first, "passed")
    assert stale_attempt is False
    assert client._status_updates[-1][2] is False
    await db.dispose()
