import pytest

from enterprise.gateway.quality.routing import route_document
from enterprise.gateway.sync.models import get_mapping, init_db
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.sync_service import (
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
    await db.close()
