"""Focused contract and ingestion tests for Document Feed 3.2 INLINE_JSON."""

from __future__ import annotations

import copy
import json
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.app import app
from enterprise.gateway.db.dialect import exec_sql
from enterprise.gateway.sync.models import get_mapping, get_outbox_by_event_id
from enterprise.gateway.quality.models import get_latest_evaluation
from enterprise.gateway.sync.ragflow_document_client import RAGFlowDocumentStub
from enterprise.gateway.sync.readiness import document_candidate_readiness
from enterprise.gateway.sync.source_adapter import SourceAdapter
from enterprise.gateway.sync.models import ExtDocumentMap
from enterprise.gateway.sync.sync_service import (
    SyncService,
    inline_json_bytes_and_sha256,
)


def _payload(*, event_id: str = "evt-json-001", content: dict | None = None) -> dict:
    document_id = "FAC-10086"
    tenant_id = "tenant-a"
    source_system = "EAM"
    version_id = "v1"
    return {
        "eventId": event_id,
        "eventType": "upsert",
        "tenantId": tenant_id,
        "sourceSystem": source_system,
        "externalDocumentId": document_id,
        "sourceVersionId": version_id,
        "fileName": "FAC-10086-MASTER.json",
        "mediaType": "application/json",
        "source": {
            "kind": "INLINE_JSON",
            "content": content
            or {
                "equipment_name": "生产一线离心机",
                "technical_profile": {"model": "CF-1200", "voltage_v": 380},
                "anything_added_later": {"value": 123},
            },
        },
        "metadata": {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "external_document_id": document_id,
            "source_system": source_system,
            "equipment_id": "EQ-CF-001",
            "fixed_asset_no": "FA-CF-001",
            "document_type": "PRODUCT_MANUAL",
            "document_version": version_id,
            "department_id": "maintenance",
            "security_level": 2,
            "business_status": "active",
            "allow_group_ids": ["maintenance"],
            "deny_group_ids": [],
        },
    }


@pytest.fixture
def inline_v3_app(isolated_gateway_db):
    app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        yield app
    finally:
        app.dependency_overrides.pop(require_service_principal, None)


def test_inline_json_serialization_is_stable():
    first, first_hash = inline_json_bytes_and_sha256({"b": 2, "a": "设备"})
    second, second_hash = inline_json_bytes_and_sha256({"a": "设备", "b": 2})

    assert first == second == '{"a":"设备","b":2}'.encode()
    assert first_hash == second_hash


@pytest.mark.asyncio
async def test_inline_json_registers_without_business_sha_and_is_idempotent(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload()
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v3/documents", json=payload)
        replay = await client.post("/enterprise/api/v3/documents", json=payload)
        duplicate = await client.post(
            "/enterprise/api/v3/documents",
            json={**payload, "eventId": "evt-json-002"},
        )
        changed = copy.deepcopy(payload)
        changed["eventId"] = "evt-json-003"
        changed["source"]["content"]["new_field"] = "different"
        conflict = await client.post("/enterprise/api/v3/documents", json=changed)
        moved = copy.deepcopy(payload)
        moved["eventId"] = "evt-json-004"
        moved["metadata"]["equipment_id"] = "EQ-OTHER"
        equipment_move = await client.post(
            "/enterprise/api/v3/documents", json=moved
        )

    async with gateway.transaction(write=False) as conn:
        mapping = await get_mapping(conn, "tenant-a", "EAM", "FAC-10086", "v1")
        outbox = await get_outbox_by_event_id(conn, payload["eventId"])

    expected_bytes, expected_hash = inline_json_bytes_and_sha256(
        payload["source"]["content"]
    )
    assert [first.status_code, replay.status_code, duplicate.status_code] == [202] * 3
    assert conflict.status_code == 409
    assert equipment_move.status_code == 409
    assert mapping is not None and outbox is not None
    assert mapping.source_kind == "INLINE_JSON"
    assert mapping.sha256 == expected_hash
    assert mapping.source_size == len(expected_bytes)
    stored = json.loads(outbox.payload)
    assert stored["sha256"] == expected_hash
    assert stored["source"]["content"] == payload["source"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda body: body.update({"sha256": "a" * 64}), "VALIDATION_ERROR"),
        (lambda body: body.update({"fileName": "facility.txt"}), "VALIDATION_ERROR"),
        (
            lambda body: body["source"]["content"].update({"api_token": "unsafe"}),
            "VALIDATION_ERROR",
        ),
        (
            lambda body: body["source"]["content"].update(
                {"equipment_id": "EQ-OTHER"}
            ),
            "DOCUMENT_METADATA_INVALID",
        ),
    ],
)
async def test_inline_json_rejects_invalid_contracts(
    inline_v3_app, mutate, expected_code
):
    payload = _payload()
    mutate(payload)
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v3/documents", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == expected_code


@pytest.mark.asyncio
async def test_inline_json_rejects_excessive_depth(inline_v3_app):
    content: dict = {"value": "leaf"}
    for _ in range(20):
        content = {"nested": content}
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/enterprise/api/v3/documents", json=_payload(content=content)
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_inline_json_rejects_request_over_two_mib(inline_v3_app):
    payload = _payload(content={"large": "x" * (2 * 1024 * 1024)})
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v3/documents", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_inline_json_readiness_requires_done_and_quality():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="FAC-10086",
        source_version_id="v1",
        event_id="evt-json-ready",
        sha256="a" * 64,
        file_name="facility.json",
        source_kind="INLINE_JSON",
        equipment_id="EQ-CF-001",
        sync_status="ready",
        event_status="completed",
        pipeline_status="RUNNING",
        business_status="active",
        current_version=1,
        source_state="AVAILABLE",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-1",
    )

    running = document_candidate_readiness(
        doc, quality_allowed=True, quality_required=True
    )
    doc.pipeline_status = "DONE"
    pending_quality = document_candidate_readiness(
        doc, quality_allowed=False, quality_required=True
    )
    ready = document_candidate_readiness(
        doc, quality_allowed=True, quality_required=True
    )

    assert running.blocking_reason == "RAGFLOW_READBACK_NOT_READY"
    assert pending_quality.blocking_reason == "DOCUMENT_QUALITY_PENDING"
    assert ready.retrievable is True


class _RejectingSourceAdapter(SourceAdapter):
    async def fetch(self, *args, **kwargs):
        raise AssertionError("INLINE_JSON must not call an external source adapter")


class _CapturingRAGFlowStub(RAGFlowDocumentStub):
    uploaded: tuple[str, bytes] | None = None

    async def upload_document(self, dataset_id, file_name, file_content, request_id=None):
        assert isinstance(file_content, bytes)
        self.uploaded = (file_name, file_content)
        return await super().upload_document(
            dataset_id, file_name, file_content, request_id=request_id
        )


class _ReparsingRAGFlowStub(_CapturingRAGFlowStub):
    async def start_parsing(
        self, dataset_id, document_ids, request_id=None
    ):
        # Simulate RAGFlow keeping an old FAIL/DONE run on the existing
        # document until the new parse request is made.
        for document_id in document_ids:
            document = self._documents.get(document_id)
            if document:
                document["data"][0]["run"] = "UNSTART"
        return await super().start_parsing(
            dataset_id, document_ids, request_id=request_id
        )


@pytest.mark.asyncio
async def test_inline_json_uses_existing_upload_parse_and_text_quality_path(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload(event_id="evt-json-worker")
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v3/documents", json=payload)
    assert response.status_code == 202

    async with gateway.transaction(write=False) as conn:
        event = await get_outbox_by_event_id(conn, payload["eventId"])
    assert event is not None

    ragflow = _CapturingRAGFlowStub()
    ragflow.run_status = "DONE"
    service = SyncService(gateway, _RejectingSourceAdapter(), ragflow)
    doc, deduplicated = await service.process_event(event)

    expected_bytes, _ = inline_json_bytes_and_sha256(payload["source"]["content"])
    ragflow_doc = ragflow._documents[doc.ragflow_document_id]["data"][0]
    assert not deduplicated
    assert doc.sync_status == "ready"
    assert ragflow.uploaded is not None
    assert ragflow.uploaded[0].endswith(".json")
    assert ragflow.uploaded[1] == expected_bytes
    assert ragflow_doc["chunk_method"] == "naive"
    assert ragflow_doc["meta_fields"]["equipment_id"] == "EQ-CF-001"
    assert ragflow_doc["meta_fields"][
        "enterprise_quality_required_capabilities"
    ] == ["text"]


@pytest.mark.asyncio
async def test_retryable_failed_event_reuses_ragflow_document_and_reparses(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload(event_id="evt-json-retry-reparse")
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        accepted = await client.post("/enterprise/api/v3/documents", json=payload)
    assert accepted.status_code == 202

    async with gateway.transaction(write=False) as conn:
        event = await get_outbox_by_event_id(conn, payload["eventId"])
    assert event is not None

    ragflow = _ReparsingRAGFlowStub()
    ragflow.run_status = "DONE"
    service = SyncService(gateway, _RejectingSourceAdapter(), ragflow)
    first_doc, _ = await service.process_event(event)
    first_ragflow_id = first_doc.ragflow_document_id
    assert first_doc.sync_status == "ready"
    assert first_ragflow_id is not None
    assert ragflow._operation_log.count("upload") == 1

    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """UPDATE ext_document_map
                  SET sync_status='failed', event_status='failed',
                      pipeline_status='FAIL',
                      last_error_code='DOCUMENT_PARSE_FAILED',
                      last_error_message='temporary parse failure',
                      last_error_retryable=1
                WHERE event_id=?""",
            (payload["eventId"],),
        )
        await exec_sql(
            conn,
            "UPDATE sync_outbox SET status='dead' WHERE event_id=?",
            (payload["eventId"],),
        )

    async with gateway.transaction(write=False) as conn:
        failed = await get_mapping(
            conn, "tenant-a", "EAM", "FAC-10086", "v1"
        )
    assert failed is not None
    _, requeued = await service.ensure_present_or_requeue(failed)
    assert requeued is True

    ragflow._documents[first_ragflow_id]["data"][0]["run"] = "FAIL"
    async with gateway.transaction(write=False) as conn:
        replay_event = await get_outbox_by_event_id(conn, payload["eventId"])
    assert replay_event is not None and replay_event.status == "pending"

    retried_doc, _ = await service.process_event(replay_event)

    assert retried_doc.sync_status == "ready"
    assert retried_doc.processing_round == 2
    assert retried_doc.ragflow_document_id == first_ragflow_id
    assert ragflow._operation_log.count("upload") == 1
    assert len(ragflow._parse_calls) == 2


@pytest.mark.asyncio
async def test_retryable_failed_event_requeues_once_and_advances_processing_round(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload(event_id="evt-json-retry-round")
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        accepted = await client.post("/enterprise/api/v3/documents", json=payload)
    assert accepted.status_code == 202

    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """UPDATE ext_document_map
                  SET sync_status='failed', event_status='failed',
                      last_error_code='RAGFLOW_UNAVAILABLE',
                      last_error_message='temporary', last_error_retryable=1
                WHERE event_id=?""",
            (payload["eventId"],),
        )
        await exec_sql(
            conn,
            "UPDATE sync_outbox SET status='dead' WHERE event_id=?",
            (payload["eventId"],),
        )

    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        replays = await asyncio.gather(
            client.post("/enterprise/api/v3/documents", json=payload),
            client.post("/enterprise/api/v3/documents", json=payload),
        )

    assert sorted(response.status_code for response in replays) == [202, 202]
    assert sorted(response.json()["deduplicated"] for response in replays) == [
        False,
        True,
    ]
    async with gateway.transaction(write=False) as conn:
        mapping = await get_mapping(
            conn, "tenant-a", "EAM", "FAC-10086", "v1"
        )
        outbox = await get_outbox_by_event_id(conn, payload["eventId"])
    assert mapping is not None
    assert mapping.processing_round == 2
    assert mapping.sync_status == "registered"
    assert mapping.last_error_retryable is False
    assert outbox is not None
    assert outbox.status == "pending"
    assert outbox.processing_round == 2


@pytest.mark.asyncio
async def test_retryable_failed_event_with_ragflow_id_reuses_document(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload(event_id="evt-json-retry-existing")
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        accepted = await client.post("/enterprise/api/v3/documents", json=payload)
    assert accepted.status_code == 202

    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """UPDATE ext_document_map
                  SET sync_status='failed', event_status='failed',
                      ragflow_dataset_id='ds-existing',
                      ragflow_document_id='doc-existing',
                      pipeline_status='FAIL',
                      last_error_code='DOCUMENT_PARSE_FAILED',
                      last_error_message='temporary parse failure',
                      last_error_retryable=1
                WHERE event_id=?""",
            (payload["eventId"],),
        )

    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        replay = await client.post("/enterprise/api/v3/documents", json=payload)

    assert replay.status_code == 202
    assert replay.json()["deduplicated"] is False
    async with gateway.transaction(write=False) as conn:
        mapping = await get_mapping(
            conn, "tenant-a", "EAM", "FAC-10086", "v1"
        )
        outbox = await get_outbox_by_event_id(conn, payload["eventId"])
    assert mapping is not None
    assert mapping.processing_round == 2
    assert mapping.ragflow_document_id == "doc-existing"
    assert mapping.pipeline_status == "UNSTART"
    assert outbox is not None
    assert outbox.status == "pending"
    assert outbox.processing_round == 2


@pytest.mark.asyncio
async def test_quality_evaluation_version_follows_processing_round(
    inline_v3_app, isolated_gateway_db
):
    gateway, _ = isolated_gateway_db
    payload = _payload(event_id="evt-json-quality-round")
    async with AsyncClient(
        transport=ASGITransport(app=inline_v3_app), base_url="http://test"
    ) as client:
        accepted = await client.post("/enterprise/api/v3/documents", json=payload)
    assert accepted.status_code == 202
    async with gateway.transaction(write=False) as conn:
        event = await get_outbox_by_event_id(conn, payload["eventId"])
    assert event is not None

    ragflow = _CapturingRAGFlowStub()
    ragflow.run_status = "DONE"
    service = SyncService(gateway, _RejectingSourceAdapter(), ragflow)
    doc, _ = await service.process_event(event)
    assert doc.processing_round == 1

    async with gateway.transaction(write=True) as conn:
        await exec_sql(
            conn,
            "UPDATE ext_document_map SET processing_round=2 WHERE id=?",
            (doc.id,),
        )
    doc.processing_round = 2
    await service._ensure_quality_evaluation(doc)

    async with gateway.transaction(write=False) as conn:
        latest = await get_latest_evaluation(
            conn, "tenant-a", "EAM", "FAC-10086", "v1"
        )
    assert latest is not None
    assert latest.evaluation_version == "2"
