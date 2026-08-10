import pytest

from enterprise.gateway.quality.routing import route_document
from enterprise.gateway.sync.models import (
    get_mapping,
    init_db,
    update_parser_application,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import (
    RetryableDocumentSyncError,
    SyncService,
    TerminalDocumentSyncError,
)
from enterprise.tests.test_wp02b import make_event


def test_safe_parser_profiles():
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


@pytest.mark.asyncio
async def test_profile_is_read_back_before_parse_and_not_activated():
    db = await init_db(":memory:")
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))

    assert doc.sync_status == "ready"
    assert doc.current_version == 0
    assert doc.parser_application_status == "executed"
    assert doc.equipment_id == "EQ-001"
    assert '"chunk_method":"naive"' in doc.parser_configured_json
    assert '"layout_recognize":"DeepDOC"' in doc.parser_executed_json
    assert client._operation_log.index("patch") < client._operation_log.index("parse")
    assert "get" in client._operation_log[
        client._operation_log.index("patch") + 1:client._operation_log.index("parse")
    ]
    await db.close()


class MismatchStub(RAGFlowDocumentStub):
    async def update_document(self, *args, **kwargs):
        result = await super().update_document(*args, **kwargs)
        result["data"][0]["parser_config"]["layout_recognize"] = "Plain Text"
        return result


class TerminalMismatchStub(RAGFlowDocumentStub):
    async def start_parsing(
        self, dataset_id, document_ids, request_id=None,
    ):
        result = await super().start_parsing(dataset_id, document_ids, request_id)
        for document_id in document_ids:
            self._documents[document_id]["data"][0]["parser_config"] = {
                "layout_recognize": "Plain Text",
            }
        return result


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
async def test_parser_readback_mismatch_fails_before_parse():
    db = await init_db(":memory:")
    client = MismatchStub()
    service = SyncService(db, SourceStub(b"manual"), client)
    event = make_event(b"manual")

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.process_event(event)

    assert error.value.code == "PARSER_APPLICATION_MISMATCH"
    doc = await get_mapping(
        db, "tenant-1", "EAM", "DOC-1", "v1",
    )
    assert doc.parser_application_status == "mismatch"
    assert client._parse_calls == []
    await db.close()


@pytest.mark.asyncio
async def test_terminal_parser_readback_mismatch_cannot_become_ready():
    db = await init_db(":memory:")
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))
    assert doc.sync_status == "ready"

    client._documents[doc.ragflow_document_id]["data"][0]["parser_config"] = {
        "layout_recognize": "Plain Text",
    }
    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.refresh_status(doc)

    assert error.value.code == "PARSER_APPLICATION_MISMATCH"
    doc = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert doc.sync_status == "review_required"
    assert doc.business_status == "review_required"
    assert doc.current_version == 0
    assert doc.parser_application_status == "mismatch"
    await db.close()


@pytest.mark.asyncio
async def test_parser_mismatch_from_retry_wait_uses_valid_failure_state():
    db = await init_db(":memory:")
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))
    await update_mapping_status(db, doc, "failed")
    await update_mapping_status(db, doc, "retry_wait")
    client._documents[doc.ragflow_document_id]["data"][0]["parser_config"] = {
        "layout_recognize": "Plain Text",
    }

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.refresh_status(doc)

    assert error.value.code == "PARSER_APPLICATION_MISMATCH"
    current = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert current.sync_status == "failed"
    assert current.sync_status != "ready"
    await db.close()


@pytest.mark.asyncio
async def test_source_digest_is_verified_before_ragflow_registration():
    db = await init_db(":memory:")
    content = b"manual"
    client = RAGFlowDocumentStub()
    service = SyncService(db, IncorrectDigestSource(content), client)

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.process_event(make_event(content))

    assert error.value.code == "DOCUMENT_HASH_MISMATCH"
    doc = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert doc.sync_status == "failed"
    assert doc.business_status == "review_required"
    assert doc.current_version == 0
    assert client._documents == {}
    await db.close()


@pytest.mark.asyncio
async def test_parse_failure_is_not_active_or_promotable():
    db = await init_db(":memory:")
    client = RAGFlowDocumentStub()
    client.run_status = "FAIL"
    service = SyncService(db, SourceStub(b"manual"), client)

    doc, _ = await service.process_event(make_event(b"manual"))

    assert doc.sync_status == "failed"
    assert doc.business_status == "review_required"
    assert doc.current_version == 0
    assert doc.last_error_code == "DOCUMENT_PARSE_FAILED"
    assert await service.promote_quality_passed_version(doc, "passed") is False
    assert not any(enabled for _, _, enabled in client._status_updates)
    await db.close()


@pytest.mark.asyncio
async def test_missing_terminal_readback_is_retryable_and_not_ready():
    db = await init_db(":memory:")
    client = MissingPostParseReadbackStub()
    service = SyncService(db, SourceStub(b"manual"), client)

    with pytest.raises(RetryableDocumentSyncError) as error:
        await service.process_event(make_event(b"manual"))

    assert error.value.code == "RAGFLOW_UNAVAILABLE"
    doc = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert doc.sync_status == "retry_wait"
    assert doc.current_version == 0
    await db.close()


@pytest.mark.asyncio
async def test_status_refresh_failure_maps_to_stable_error():
    db = await init_db(":memory:")
    client = RefreshFailureStub()
    client.run_status = "RUNNING"
    service = SyncService(db, SourceStub(b"manual"), client)
    doc, _ = await service.process_event(make_event(b"manual"))
    client.fail_reads = True

    with pytest.raises(RetryableDocumentSyncError) as error:
        await service.refresh_status(doc)

    assert error.value.code == "RAGFLOW_UNAVAILABLE"
    await db.close()


@pytest.mark.asyncio
async def test_lifecycle_does_not_commit_when_ragflow_reports_document_error():
    db = await init_db(":memory:")
    client = BatchStatusErrorStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)
    doc, _ = await service.process_event(make_event(b"manual"))

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.disable_document("tenant-1", "EAM", "DOC-1")

    assert error.value.code == "RAGFLOW_API_INCOMPATIBLE"
    current = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert current.sync_status == "ready"
    assert current.business_status == "active"
    assert doc.current_version == 0
    await db.close()


@pytest.mark.asyncio
async def test_reindex_rejects_legacy_unverified_parser_before_parse():
    db = await init_db(":memory:")
    client = RAGFlowDocumentStub()
    client.run_status = "DONE"
    service = SyncService(db, SourceStub(b"manual"), client)
    doc, _ = await service.process_event(make_event(b"manual"))
    await update_parser_application(db, doc, status="legacy_unverified")

    with pytest.raises(TerminalDocumentSyncError) as error:
        await service.reindex_document("tenant-1", "EAM", "DOC-1", "v1")

    assert error.value.code == "PARSER_APPLICATION_UNVERIFIABLE"
    assert client._parse_calls == [(doc.ragflow_dataset_id, [doc.ragflow_document_id])]
    await db.close()


@pytest.mark.asyncio
async def test_only_quality_passed_executed_latest_version_is_promoted():
    db = await init_db(":memory:")
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
    first = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    assert first.current_version == 1
    assert second.current_version == 0

    assert await second_service.promote_quality_passed_version(second, "passed") is True
    first = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v1")
    second = await get_mapping(db, "tenant-1", "EAM", "DOC-1", "v2")
    assert first.current_version == 0
    assert first.business_status == "superseded"
    assert second.current_version == 1
    assert any(enabled is False for _, _, enabled in client._status_updates)
    stale_attempt = await first_service.promote_quality_passed_version(first, "passed")
    assert stale_attempt is False
    assert client._status_updates[-1][2] is False
    await db.close()
