"""P0 FILE_SHARE v3 status truth and stable-location tests."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.app import app
from enterprise.gateway.query import v2_router
from enterprise.gateway.quality import models as quality_models
from enterprise.gateway.sync import v3_router
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    OutboxEvent,
    get_mapping,
    insert_mapping,
    update_mapping_status,
    update_parser_application,
)
from enterprise.gateway.sync.sync_service import SyncService, _ragflow_file_name


def _metadata(
    document_id: str,
    *,
    tenant_id: str = "tenant-a",
    source_system: str = "DEMO",
    source_version_id: str = "v1",
) -> dict:
    return {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "external_document_id": document_id,
        "source_system": source_system,
        "equipment_id": "EQ-001",
        "fixed_asset_no": "FA-001",
        "document_type": "PRODUCT_MANUAL",
        "document_version": source_version_id,
        "department_id": "maintenance",
        "security_level": 2,
        "business_status": "active",
        "allow_group_ids": ["maintenance"],
        "deny_group_ids": [],
        "page_count": 1,
    }


def _payload(
    *,
    event_id: str = "evt-v3-001",
    document_id: str = "DOC-V3-001",
    tenant_id: str = "tenant-a",
    source_system: str = "DEMO",
    source_version_id: str = "v1",
    event_type: str = "upsert",
) -> dict:
    digest = hashlib.sha256(
        f"{tenant_id}:{source_system}:{document_id}:{source_version_id}".encode()
    ).hexdigest()
    return {
        "eventId": event_id,
        "eventType": event_type,
        "tenantId": tenant_id,
        "sourceSystem": source_system,
        "externalDocumentId": document_id,
        "sourceVersionId": source_version_id,
        "sha256": digest,
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "test-root",
            "relativePath": "manual.pdf",
            "size": 123,
            "etag": "etag-v1",
        },
        "metadata": _metadata(
            document_id,
            tenant_id=tenant_id,
            source_system=source_system,
            source_version_id=source_version_id,
        ),
    }


def test_real_ragflow_metadata_declares_quality_expectations():
    doc = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="DEMO",
        external_document_id="DOC-V3-001",
        source_version_id="v1",
        event_id="evt-v3-001",
        sha256="a" * 64,
        file_name="manual.pdf",
        document_type="PRODUCT_MANUAL",
        equipment_id="EQ-001",
        fixed_asset_no="FA-001",
    )
    event = OutboxEvent(
        event_id=doc.event_id,
        event_type="upsert",
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        payload="{}",
    )

    metadata = SyncService._external_meta_fields(doc, event)

    assert metadata["enterprise_quality_expected_tables"] == []
    assert metadata["enterprise_quality_ground_truth_fields"] == {
        "equipment_id": "EQ-001",
        "fixed_asset_no": "FA-001",
    }
    assert metadata["enterprise_quality_citation_expected"] is False
    assert metadata["enterprise_quality_required_capabilities"] == [
        "text",
        "position",
        "key_field",
    ]


def test_ragflow_internal_name_is_stable_and_business_document_unique():
    first = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-001",
        source_version_id="v1",
        event_id="evt-001",
        sha256="a" * 64,
        file_name="manual.pdf",
    )
    second = ExtDocumentMap(
        tenant_id="tenant-a",
        source_system="EAM",
        external_document_id="DOC-002",
        source_version_id="v1",
        event_id="evt-002",
        sha256="b" * 64,
        file_name="manual.pdf",
    )

    first_name = _ragflow_file_name(first, first.file_name)

    assert first_name == _ragflow_file_name(first, first.file_name)
    assert first_name.endswith(".pdf")
    assert first_name != _ragflow_file_name(second, second.file_name)


@pytest.fixture
def v3_app(isolated_gateway_db):
    app.dependency_overrides[require_service_principal] = lambda: ServicePrincipal(
        source_system="service"
    )
    try:
        yield app
    finally:
        app.dependency_overrides.pop(require_service_principal, None)


def _assert_status_url(
    status_url: str,
    *,
    tenant_id: str,
    source_system: str,
    document_id: str,
    source_version_id: str,
) -> None:
    parsed = urlsplit(status_url)
    assert parsed.scheme == ""
    assert parsed.netloc == ""
    assert parsed.path.startswith("/enterprise/api/v3/documents/")
    assert parsed.path.endswith("/status")
    encoded_document_id = parsed.path[
        len("/enterprise/api/v3/documents/") : -len("/status")
    ]
    assert unquote(encoded_document_id) == document_id
    assert parse_qs(parsed.query, keep_blank_values=True) == {
        "tenantId": [tenant_id],
        "sourceSystem": [source_system],
        "sourceVersionId": [source_version_id],
    }


@pytest.mark.asyncio
async def test_every_v3_202_acceptance_path_returns_the_same_status_url(
    v3_app, isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    payload = _payload()
    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    ) as client:
        first = await client.post("/enterprise/api/v3/documents", json=payload)
        replay = await client.post("/enterprise/api/v3/documents", json=payload)
        duplicate_payload = {**payload, "eventId": "evt-v3-002"}
        duplicate = await client.post(
            "/enterprise/api/v3/documents", json=duplicate_payload
        )

        existing = await get_mapping(db, "tenant-a", "DEMO", "DOC-V3-001", "v1")

        class ReindexStub:
            async def reindex_document(
                self, tenant_id, source_system, external_document_id, source_version_id
            ):
                return await get_mapping(
                    db, tenant_id, source_system, external_document_id, source_version_id
                )

        monkeypatch.setattr(v3_router, "_sync_service", lambda _db: ReindexStub())
        reindex_payload = _payload(
            event_id="evt-v3-reindex", event_type="reindex"
        )
        reindex = await client.post(
            "/enterprise/api/v3/documents", json=reindex_payload
        )
        reindex_replay = await client.post(
            "/enterprise/api/v3/documents", json=reindex_payload
        )

    assert existing is not None
    responses = [first, replay, duplicate, reindex, reindex_replay]
    assert [response.status_code for response in responses] == [202] * 5
    status_urls = {response.json()["statusUrl"] for response in responses}
    assert len(status_urls) == 1
    _assert_status_url(
        first.json()["statusUrl"],
        tenant_id="tenant-a",
        source_system="DEMO",
        document_id="DOC-V3-001",
        source_version_id="v1",
    )
    assert reindex.json()["operationId"] == "evt-v3-reindex"
    assert reindex_replay.json()["deduplicated"] is True


@pytest.mark.asyncio
async def test_status_url_encodes_identity_and_points_to_exact_status_resource(
    v3_app,
):
    payload = _payload(
        event_id="evt-v3-encoded",
        tenant_id="tenant / A",
        source_system="EAM / North",
        document_id="DOC / 001?x",
        source_version_id="version / 1?x",
    )
    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    ) as client:
        response = await client.post("/enterprise/api/v3/documents", json=payload)
        status_response = await client.get(response.json()["statusUrl"])

    assert response.status_code == 202
    assert status_response.status_code == 200
    assert status_response.json()["externalDocumentId"] == "DOC / 001?x"
    _assert_status_url(
        response.json()["statusUrl"],
        tenant_id="tenant / A",
        source_system="EAM / North",
        document_id="DOC / 001?x",
        source_version_id="version / 1?x",
    )
    assert "%2F" in response.json()["statusUrl"].split("?")[1]
    assert "%2F" in response.json()["statusUrl"]


async def _insert_file_share_document(
    db,
    *,
    document_id: str,
    source_version_id: str = "v1",
    sync_status: str = "ready",
    current_version: int = 1,
    business_status: str = "active",
    parser_status: str = "executed",
    pipeline_status: str = "DONE",
    event_status: str = "completed",
    source_state: str = "AVAILABLE",
    error_code: str | None = None,
    error_message: str | None = None,
) -> ExtDocumentMap:
    doc = await insert_mapping(
        db,
        ExtDocumentMap(
            tenant_id="tenant-a",
            source_system="DEMO",
            external_document_id=document_id,
            source_version_id=source_version_id,
            event_id=f"evt-{document_id}-{source_version_id}",
            sha256=hashlib.sha256(document_id.encode()).hexdigest(),
            file_name="manual.pdf",
            media_type="application/pdf",
            document_type="PRODUCT_MANUAL",
            source_kind="FILE_SHARE",
            storage_root_id="test-root",
            relative_path="manual.pdf",
            source_size=123,
            source_etag="etag-v1",
            equipment_id="EQ-001",
            fixed_asset_no="FA-001",
            department_id="maintenance",
            security_level=2,
            allow_group_ids=json.dumps(["maintenance"]),
            deny_group_ids="[]",
            ragflow_dataset_id="dataset-1",
            ragflow_document_id=f"rag-doc-{document_id}",
            sync_status=sync_status,
            business_status=business_status,
            current_version=current_version,
            parser_application_status=parser_status,
            ingest_state="READY" if sync_status == "ready" else "PARSING",
            source_state=source_state,
            event_status=event_status,
        ),
    )
    await update_mapping_status(
        db,
        doc,
        sync_status,
        pipeline_status=pipeline_status,
        event_status=event_status,
        business_status=business_status,
        current_version=current_version,
        source_state=source_state,
        error_code=error_code,
        error_message=error_message,
    )
    if parser_status != "legacy_unverified":
        await update_parser_application(db, doc, status=parser_status)
    return doc


async def _complete_quality(db, doc: ExtDocumentMap, status: str):
    evaluation = await quality_models.get_or_create_evaluation(
        db,
        tenant_id=doc.tenant_id,
        source_system=doc.source_system,
        external_document_id=doc.external_document_id,
        source_version_id=doc.source_version_id,
        ragflow_dataset_id=doc.ragflow_dataset_id,
        ragflow_document_id=doc.ragflow_document_id,
        routing={},
    )
    await quality_models.complete_evaluation(
        db,
        evaluation.id,
        parse_quality_status=status,
        quality_reasons=[],
        metrics_json={
            "parse_success": True,
            "chunk_count": 1,
            "effective_text_coverage": 1.0,
            "garbled_char_ratio": 0.0,
            "position_coverage": 1.0,
            "required_capabilities": ["text"],
            "quality_expectations": {"declarations_complete": True},
            "parserApplication": {"state": "executed", "readbackMatch": True},
        },
        parse_repeatability_hash="parse-hash",
        e2e_repeatability_hash="e2e-hash",
        artifact_hash="artifact-hash",
        enterprise_commit="commit",
        enterprise_worktree_dirty=False,
        ragflow_source_tag="v0.26.4",
        ragflow_source_commit="commit",
        thresholds_version="1",
        thresholds_digest="digest",
    )


def _user(*, groups: tuple[str, ...]) -> UserPrincipal:
    return UserPrincipal(
        tenant_id="tenant-a",
        business_user_id="user-1",
        subject="user-1",
        department_ids=("maintenance",),
        group_ids=groups,
        security_level=2,
        mapping_status="active",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document_id", "kwargs", "quality", "reason"),
    [
        (
            "DOC-PARSER-BLOCKED",
            {"parser_status": "mismatch"},
            "passed",
            "PARSER_READBACK_NOT_READY",
        ),
        (
            "DOC-QUALITY-BLOCKED",
            {},
            "failed",
            "DOCUMENT_QUALITY_FAILED",
        ),
        (
            "DOC-QUALITY-PENDING",
            {},
            None,
            "DOCUMENT_QUALITY_PENDING",
        ),
        (
            "DOC-NON-CURRENT",
            {"current_version": 0},
            "passed",
            "DOCUMENT_NOT_CURRENT_VERSION",
        ),
    ],
)
async def test_status_never_reports_retrievable_for_incomplete_document_facts(
    v3_app, isolated_gateway_db, monkeypatch, document_id, kwargs, quality, reason
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "true")
    doc = await _insert_file_share_document(db, document_id=document_id, **kwargs)
    if quality:
        await _complete_quality(db, doc, quality)

    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/enterprise/api/v3/documents/{document_id}/status",
            params={"tenantId": "tenant-a", "sourceSystem": "DEMO", "sourceVersionId": "v1"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["retrievable"] is False
    assert body["pipelineStatus"] == doc.pipeline_status
    assert body["parseCompleted"] is (doc.parser_application_status == "executed")
    assert body["indexCompleted"] is (str(doc.pipeline_status).upper() in {"DONE", "3"})
    assert body["errorCode"] is None
    assert body["readiness"]["blockingReason"] == reason


@pytest.mark.asyncio
async def test_failed_status_error_is_stable_and_secret_free(
    v3_app, isolated_gateway_db
):
    db, _ = isolated_gateway_db
    await _insert_file_share_document(
        db,
        document_id="DOC-FAILED",
        sync_status="failed",
        error_code="INTERNAL_SECRET_DETAIL",
        error_message="sensitive details",
    )

    async with AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/enterprise/api/v3/documents/DOC-FAILED/status",
            params={
                "tenantId": "tenant-a",
                "sourceSystem": "DEMO",
                "sourceVersionId": "v1",
            },
        )

    assert response.json()["error"] == {
        "code": "DOCUMENT_SYNC_FAILED",
        "message": "Document synchronization failed",
        "retryable": True,
    }
    assert response.json()["errorCode"] == "DOCUMENT_SYNC_FAILED"
    assert "sensitive details" not in response.text


@pytest.mark.asyncio
async def test_retrievable_is_document_readiness_only_and_acl_is_applied_later(
    v3_app, isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "true")
    doc = await _insert_file_share_document(db, document_id="DOC-ACL-SEMANTICS")
    await _complete_quality(db, doc, "passed")

    from enterprise.gateway.sync.readiness import document_candidate_readiness

    readiness = document_candidate_readiness(
        doc, quality_allowed=True, quality_required=True
    )
    assert readiness.retrievable is True

    blocked = await _insert_file_share_document(
        db, document_id="DOC-ACL-PARSER", parser_status="mismatch"
    )
    await _complete_quality(db, blocked, "passed")
    conversation = {
        "equipment_id": "EQ-001",
        "fixed_asset_no": "FA-001",
        "asset_id": None,
    }
    status_client = AsyncClient(
        transport=ASGITransport(app=v3_app), base_url="http://test"
    )
    async with status_client as client:
        status = await client.get(
            "/enterprise/api/v3/documents/DOC-ACL-SEMANTICS/status",
            params={
                "tenantId": "tenant-a",
                "sourceSystem": "DEMO",
                "sourceVersionId": "v1",
            },
        )
    allowed_scope, allowed_docs = await v2_router._context_scope(
        db, _user(groups=("maintenance",)), conversation
    )
    denied_scope, _ = await v2_router._context_scope(
        db, _user(groups=("other",)), conversation
    )
    status_body = status.json()
    assert status_body["retrievable"] is True
    assert status_body["pipelineStatus"] == "DONE"
    assert status_body["parseCompleted"] is True
    assert status_body["indexCompleted"] is True
    assert status_body["errorCode"] is None
    assert (
        v3_router.document_candidate_readiness_from_db
        is v2_router.document_candidate_readiness_from_db
    )
    assert allowed_scope.document_ids == (doc.ragflow_document_id,)
    assert set(allowed_docs) == {doc.ragflow_document_id}
    assert denied_scope.document_ids == ()
