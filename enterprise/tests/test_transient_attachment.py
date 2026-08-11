"""Unit and Gateway contract tests for transient conversation attachments.

These tests exercise the Enterprise service boundary with an in-memory object
store. They are not Integration-profile evidence for MinIO, RAGFlow or M2.
"""

from __future__ import annotations

import base64
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway import app as app_module
from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config
from enterprise.gateway.query import v2_store
from enterprise.gateway.sync.models import init_db
from enterprise.gateway.sync.source_adapter import S3SourceAdapter, SourceFile
from enterprise.gateway.sync.transient_attachment import (
    TransientAttachmentError,
    TransientAttachmentService,
    attachment_max_encoded_length,
    decode_attachment_content,
    get_db,
    get_storage,
    optional_user_principal,
    router,
)


class MemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.put_failures = 0
        self.fetch_failures = 0
        self.delete_failures = 0
        self.fetch_calls = 0
        self.delete_calls = 0

    async def put_object(self, bucket, object_key, content, media_type):
        if self.put_failures:
            self.put_failures -= 1
            raise RuntimeError("temporary storage failure")
        self.objects[(bucket, object_key)] = (content, media_type)

    async def fetch(self, bucket, object_key, expected_sha256=None):
        self.fetch_calls += 1
        if self.fetch_failures:
            self.fetch_failures -= 1
            raise RuntimeError("temporary storage failure")
        content, media_type = self.objects[(bucket, object_key)]
        return SourceFile(
            content=content,
            file_name=object_key.rsplit("/", 1)[-1],
            media_type=media_type,
            size=len(content),
            sha256=expected_sha256,
        )

    async def delete_object(self, bucket, object_key):
        self.delete_calls += 1
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("temporary storage failure")
        self.objects.pop((bucket, object_key), None)


def _principal(
    tenant_id: str = "tenant-a",
    business_user_id: str = "user-a",
    capabilities: tuple[str, ...] = ("ask", "list_sessions", "view_citations"),
) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        subject=business_user_id,
        department_ids=("maintenance",),
        group_ids=("maintenance",),
        capabilities=capabilities,
    )


async def _conversation(db, principal: UserPrincipal, conversation_id: str = "conversation-a"):
    await v2_store.ensure_schema(db)
    return await v2_store.create_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        equipment_id=None,
        fixed_asset_no=None,
        fault_code=None,
    )


def _attachment_payload(content: bytes) -> bytes:
    return json.dumps(
        {
            "fileName": "manual.pdf",
            "mediaType": "application/pdf",
            "content": base64.b64encode(content).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _asgi_request(
    application,
    path: str,
    body_chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict, int]:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    ]
    receive_calls = 0
    sent: list[dict] = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    raw_path = path.encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": b"",
        "headers": headers or [(b"content-type", b"application/json")],
        "client": ("testclient", 50000),
        "server": ("gateway.test", 80),
        "root_path": "",
        "state": {},
    }
    await application(scope, receive, send)
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return response_start["status"], json.loads(response_body), receive_calls


@pytest.mark.asyncio
async def test_s3_adapter_writes_and_deletes_without_exposing_client(storage_env):
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def put_object(self, **kwargs):
            calls.append(("put", kwargs))

        def delete_object(self, **kwargs):
            calls.append(("delete", kwargs))

    adapter = S3SourceAdapter(
        max_size_bytes=1024,
    )
    adapter._client = lambda: FakeClient()
    await adapter.put_object("bucket", "transient/a.pdf", b"bytes", "application/pdf")
    await adapter.delete_object("bucket", "transient/a.pdf")

    assert [call[0] for call in calls] == ["put", "delete"]
    assert calls[0][1]["Body"] == b"bytes"
    assert calls[0][1]["ContentType"] == "application/pdf"
    assert set(calls[0][1]) == {"Bucket", "Key", "Body", "ContentType"}


@pytest.fixture
def storage_env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "attachment-test-bucket")
    monkeypatch.setenv("S3_ENDPOINT", "http://minio.internal:9000")
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_TTL_SECONDS", "86400")
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", "10485760")


@pytest.mark.asyncio
async def test_attachment_post_is_501_by_default_without_reading_or_resolving_dependencies(
    monkeypatch,
):
    monkeypatch.setattr(config, "transient_attachments_enabled", False)

    async def fail_dependency():
        raise AssertionError("attachment dependency must not run when disabled")

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, fail_dependency)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        fail_dependency,
    )

    status, body, receive_calls = await _asgi_request(
        app_module.app,
        "/enterprise/api/v2/conversations/conversation-a/attachments",
        [b"this body must not be read"],
    )

    assert status == 501
    assert body["code"] == "ATTACHMENT_NOT_IMPLEMENTED"
    assert body["message"] == "Transient attachment is planned but not enabled"
    assert body["retryable"] is False
    assert isinstance(body["requestId"], str) and body["requestId"]
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_disabled_feature_gates_ticket_and_download_before_dependencies(
    monkeypatch,
):
    monkeypatch.setattr(config, "transient_attachments_enabled", False)

    async def fail_dependency():
        raise AssertionError("attachment dependency must not run when disabled")

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, fail_dependency)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        fail_dependency,
    )
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        optional_user_principal,
        fail_dependency,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://gateway.test"
    ) as client:
        ticket = await client.post(
            "/enterprise/api/v2/attachments/attachment-a/ticket"
        )
        download = await client.get(
            "/enterprise/api/v2/attachments/attachment-a/download/ticket-a"
        )

    for response in (ticket, download):
        assert response.status_code == 501
        assert response.json()["code"] == "ATTACHMENT_NOT_IMPLEMENTED"


@pytest.mark.asyncio
async def test_enabled_attachment_post_keeps_201(storage_env, monkeypatch):
    monkeypatch.setattr(config, "transient_attachments_enabled", True)
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, lambda: db)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, lambda: storage)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        lambda: owner,
    )
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        optional_user_principal,
        lambda: owner,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://gateway.test"
    ) as client:
        response = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json={
                "fileName": "manual.pdf",
                "mediaType": "application/pdf",
                "content": base64.b64encode(b"enabled-bytes").decode("ascii"),
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["indexPolicy"] == "never"
    await db.close()


@pytest.mark.asyncio
async def test_attachment_request_content_length_over_limit_is_standard_413(
    monkeypatch,
):
    monkeypatch.setattr(config, "transient_attachments_enabled", True)
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", "3")
    body = _attachment_payload(b"A" * 50_000)
    assert len(body) > attachment_max_encoded_length() + 64 * 1024

    async def fail_dependency():
        raise AssertionError("oversized request must be rejected before dependencies")

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, fail_dependency)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        fail_dependency,
    )
    status, response, _ = await _asgi_request(
        app_module.app,
        "/enterprise/api/v2/conversations/conversation-a/attachments",
        [body],
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    )

    assert status == 413
    assert response["code"] == "ATTACHMENT_TOO_LARGE"
    assert response["retryable"] is False
    assert response["requestId"]


@pytest.mark.asyncio
async def test_attachment_request_body_limit_counts_forged_content_length(
    monkeypatch,
):
    monkeypatch.setattr(config, "transient_attachments_enabled", True)
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", "3")
    body = _attachment_payload(b"B" * 50_000)

    async def fail_dependency():
        raise AssertionError("oversized request must be rejected before dependencies")

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, fail_dependency)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        fail_dependency,
    )
    status, response, _ = await _asgi_request(
        app_module.app,
        "/enterprise/api/v2/conversations/conversation-a/attachments",
        [body[:100], body[100:]],
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
        ],
    )

    assert status == 413
    assert response["code"] == "ATTACHMENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_attachment_request_body_limit_counts_chunked_body_without_content_length(
    monkeypatch,
):
    monkeypatch.setattr(config, "transient_attachments_enabled", True)
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", "3")
    body = _attachment_payload(b"C" * 50_000)

    async def fail_dependency():
        raise AssertionError("oversized request must be rejected before dependencies")

    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, fail_dependency)
    monkeypatch.setitem(app_module.app.dependency_overrides, get_storage, fail_dependency)
    monkeypatch.setitem(
        app_module.app.dependency_overrides,
        require_user_principal,
        fail_dependency,
    )
    midpoint = len(body) // 2
    status, response, _ = await _asgi_request(
        app_module.app,
        "/enterprise/api/v2/conversations/conversation-a/attachments",
        [body[:midpoint], body[midpoint:]],
    )

    assert status == 413
    assert response["code"] == "ATTACHMENT_TOO_LARGE"


def test_attachment_encoded_and_decoded_limits_are_both_enforced(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", "4")
    assert attachment_max_encoded_length() == 8
    assert decode_attachment_content(base64.b64encode(b"1234").decode("ascii")) == b"1234"

    with pytest.raises(TransientAttachmentError) as decoded_error:
        decode_attachment_content(base64.b64encode(b"12345").decode("ascii"))
    assert decoded_error.value.code == "ATTACHMENT_TOO_LARGE"
    assert decoded_error.value.status_code == 413

    with pytest.raises(TransientAttachmentError) as encoded_error:
        decode_attachment_content("A" * (attachment_max_encoded_length() + 4))
    assert encoded_error.value.code == "ATTACHMENT_TOO_LARGE"
    assert encoded_error.value.status_code == 413


@pytest.mark.asyncio
async def test_attachment_validation_rejects_mime_extension_and_size(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    service = TransientAttachmentService(db, storage)

    with pytest.raises(TransientAttachmentError) as mime_error:
        await service.create(
            tenant_id="tenant-a",
            conversation_id="conversation-a",
            business_user_id="user-a",
            file_name="manual.pdf",
            media_type="text/plain",
            content=b"not a pdf",
        )
    assert mime_error.value.code == "ATTACHMENT_MIME_NOT_ALLOWED"

    with pytest.raises(TransientAttachmentError) as extension_error:
        await service.create(
            tenant_id="tenant-a",
            conversation_id="conversation-a",
            business_user_id="user-a",
            file_name="manual.exe",
            media_type="application/octet-stream",
            content=b"blocked",
        )
    assert extension_error.value.code == "ATTACHMENT_EXTENSION_INVALID"

    import os

    previous = os.environ["ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES"]
    os.environ["ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES"] = "3"
    try:
        with pytest.raises(TransientAttachmentError) as size_error:
            await service.create(
                tenant_id="tenant-a",
                conversation_id="conversation-a",
                business_user_id="user-a",
                file_name="manual.pdf",
                media_type="application/pdf",
                content=b"1234",
            )
    finally:
        os.environ["ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES"] = previous
    assert size_error.value.code == "ATTACHMENT_TOO_LARGE"
    await db.close()


@pytest.mark.asyncio
async def test_ticket_is_tenant_bound_and_repeated_download_is_denied(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    service = TransientAttachmentService(db, storage)
    owner = _principal()
    other_tenant = _principal("tenant-b", "user-b")
    await _conversation(db, owner)
    record, ticket = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"attachment-bytes",
    )

    with pytest.raises(TransientAttachmentError) as forbidden:
        await service.download(
            attachment_id=record.attachment_id,
            token=ticket.token,
            principal=other_tenant,
        )
    assert forbidden.value.code == "ATTACHMENT_FORBIDDEN"

    downloaded = await service.download(
        attachment_id=record.attachment_id,
        token=ticket.token,
        principal=owner,
    )
    assert downloaded.content == b"attachment-bytes"
    with pytest.raises(TransientAttachmentError) as repeated:
        await service.download(
            attachment_id=record.attachment_id,
            token=ticket.token,
            principal=owner,
        )
    assert repeated.value.code == "ATTACHMENT_DOWNLOAD_LIMIT"

    async with db.execute(
        "SELECT action, outcome, metadata_json FROM ext_transient_attachment_audit"
    ) as cursor:
        audit_rows = await cursor.fetchall()
    assert any(row["action"] == "download" and row["outcome"] == "accepted" for row in audit_rows)
    assert all("attachment-bytes" not in row["metadata_json"] for row in audit_rows)
    await db.close()


@pytest.mark.asyncio
async def test_archived_conversation_keeps_ticket_issue_denied(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)
    service = TransientAttachmentService(db, storage)
    record, _ = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"archived-bytes",
    )
    await v2_store.archive_conversation(
        db,
        conversation_id="conversation-a",
        tenant_id=owner.tenant_id,
        business_user_id=owner.business_user_id,
    )

    with pytest.raises(TransientAttachmentError) as archived:
        await service.issue_download_ticket(
            attachment_id=record.attachment_id,
            tenant_id=owner.tenant_id,
            business_user_id=owner.business_user_id,
        )
    assert archived.value.code == "CONVERSATION_ARCHIVED"
    assert archived.value.status_code == 409
    await db.close()


@pytest.mark.asyncio
async def test_conversation_operational_error_returns_503_without_issuing_ticket(
    storage_env,
):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)
    service = TransientAttachmentService(db, storage)
    record, _ = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"unavailable-bytes",
    )
    async with db.execute(
        "SELECT COUNT(*) AS count FROM ext_transient_attachment_ticket"
    ) as cursor:
        before = (await cursor.fetchone())["count"]
    await db.execute("DROP TABLE ext_v2_conversation")
    await db.commit()

    with pytest.raises(TransientAttachmentError) as unavailable:
        await service.issue_download_ticket(
            attachment_id=record.attachment_id,
            tenant_id=owner.tenant_id,
            business_user_id=owner.business_user_id,
        )
    assert unavailable.value.code == "CONVERSATION_UNAVAILABLE"
    assert unavailable.value.status_code == 503
    assert unavailable.value.retryable is True
    async with db.execute(
        "SELECT COUNT(*) AS count FROM ext_transient_attachment_ticket"
    ) as cursor:
        after = (await cursor.fetchone())["count"]
    assert after == before
    await db.close()


@pytest.mark.asyncio
async def test_unavailable_conversation_status_returns_503_without_issuing_ticket(
    storage_env,
):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)
    service = TransientAttachmentService(db, storage)
    record, _ = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"status-bytes",
    )
    await db.execute(
        "UPDATE ext_v2_conversation SET status='unavailable' "
        "WHERE conversation_id=? AND tenant_id=? AND business_user_id=?",
        ("conversation-a", owner.tenant_id, owner.business_user_id),
    )
    await db.commit()

    with pytest.raises(TransientAttachmentError) as unavailable:
        await service.issue_download_ticket(
            attachment_id=record.attachment_id,
            tenant_id=owner.tenant_id,
            business_user_id=owner.business_user_id,
        )
    assert unavailable.value.code == "CONVERSATION_UNAVAILABLE"
    assert unavailable.value.status_code == 503
    await db.close()


@pytest.mark.asyncio
async def test_expired_ticket_and_fetch_retry(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    clock = [datetime(2026, 8, 10, tzinfo=timezone.utc)]
    service = TransientAttachmentService(db, storage, now_fn=lambda: clock[0])
    await _conversation(db, _principal())
    record, ticket = await service.create(
        tenant_id="tenant-a",
        conversation_id="conversation-a",
        business_user_id="user-a",
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"retryable-bytes",
    )
    expired_ticket = await service.issue_download_ticket(
        attachment_id=record.attachment_id,
        tenant_id="tenant-a",
        business_user_id="user-a",
    )
    storage.fetch_failures = 1
    downloaded = await service.download(
        attachment_id=record.attachment_id,
        token=ticket.token,
    )
    assert downloaded.content == b"retryable-bytes"
    assert storage.fetch_calls == 2

    clock[0] += timedelta(days=2)
    with pytest.raises(TransientAttachmentError) as expired:
        await service.download(
            attachment_id=record.attachment_id,
            token=expired_ticket.token,
        )
    assert expired.value.code == "ATTACHMENT_EXPIRED"
    await db.close()


@pytest.mark.asyncio
async def test_expired_cleanup_retries_delete_and_records_metadata(storage_env, monkeypatch):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_TTL_SECONDS", "1")
    clock = [datetime(2026, 8, 10, tzinfo=timezone.utc)]
    service = TransientAttachmentService(db, storage, now_fn=lambda: clock[0])
    await _conversation(db, _principal())
    record, _ = await service.create(
        tenant_id="tenant-a",
        conversation_id="conversation-a",
        business_user_id="user-a",
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"cleanup-bytes",
    )
    clock[0] += timedelta(seconds=2)
    storage.delete_failures = 3
    first = await service.cleanup_expired()
    assert first["failed"] == 1
    async with db.execute(
        "SELECT status, delete_attempts FROM ext_transient_attachment WHERE attachment_id=?",
        (record.attachment_id,),
    ) as cursor:
        pending = await cursor.fetchone()
    assert pending["status"] == "delete_retry"
    assert pending["delete_attempts"] >= 3

    await db.execute(
        "UPDATE ext_transient_attachment SET next_retry_at=NULL WHERE attachment_id=?",
        (record.attachment_id,),
    )
    await db.commit()
    second = await service.cleanup_expired()
    assert second["deleted"] == 1
    assert storage.objects == {}
    async with db.execute(
        "SELECT action, outcome FROM ext_transient_attachment_audit WHERE attachment_id=?",
        (record.attachment_id,),
    ) as cursor:
        actions = await cursor.fetchall()
    assert any(row["action"] == "cleanup" and row["outcome"] == "failed" for row in actions)
    assert any(row["action"] == "cleanup" and row["outcome"] == "accepted" for row in actions)
    await db.close()


@pytest.mark.asyncio
async def test_gateway_contract_validates_permissions_and_hides_storage_url(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    other = _principal("tenant-b", "user-b")
    await _conversation(db, owner)

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_storage] = lambda: storage
    application.dependency_overrides[require_user_principal] = lambda: owner
    application.dependency_overrides[optional_user_principal] = lambda: owner

    payload = {
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "content": base64.b64encode(b"gateway-bytes").decode("ascii"),
    }
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://gateway.test"
    ) as client:
        created = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json=payload,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["indexPolicy"] == "never"
        assert "minio.internal" not in body["downloadUrl"]
        assert "S3_ENDPOINT" not in json.dumps(body)

        wrong_mime = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json={**payload, "mediaType": "text/plain"},
        )
        assert wrong_mime.status_code == 422
        assert wrong_mime.json()["code"] == "ATTACHMENT_MIME_NOT_ALLOWED"

        application.dependency_overrides[optional_user_principal] = lambda: other
        unauthorized = await client.get(body["downloadUrl"])
        assert unauthorized.status_code == 403
        assert unauthorized.json()["code"] == "ATTACHMENT_FORBIDDEN"

        application.dependency_overrides[optional_user_principal] = lambda: None
        downloaded = await client.get(body["downloadUrl"])
        assert downloaded.status_code == 200
        assert downloaded.content == b"gateway-bytes"
        repeated = await client.get(body["downloadUrl"])
        assert repeated.status_code == 410
        assert repeated.json()["code"] == "ATTACHMENT_DOWNLOAD_LIMIT"

        application.dependency_overrides[require_user_principal] = lambda: other
        cross_tenant = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json=payload,
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["code"] == "CONVERSATION_NOT_FOUND"

    await db.close()


@pytest.mark.asyncio
async def test_disabled_feature_does_not_start_cleanup_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.setenv("RAGFLOW_API_KEY", "test-key")
    monkeypatch.setenv("ENTERPRISE_SYNC_DB_PATH", str(tmp_path / "lifespan.db"))
    monkeypatch.setattr(app_module.config, "worker_enabled", True)
    monkeypatch.setattr(app_module.config, "quality_worker_enabled", False)
    monkeypatch.setattr(app_module.config, "transient_attachments_enabled", False)

    class IdleWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def run_forever(self, *args):
            await asyncio.Event().wait()

    cleanup_constructions = 0

    class ForbiddenCleanupWorker:
        def __init__(self, *args, **kwargs):
            nonlocal cleanup_constructions
            cleanup_constructions += 1

    monkeypatch.setattr(app_module, "OutboxWorker", IdleWorker)
    monkeypatch.setattr(app_module, "StatusReconciler", IdleWorker)
    monkeypatch.setattr(
        app_module,
        "TransientAttachmentCleanupWorker",
        ForbiddenCleanupWorker,
    )

    async with app_module.lifespan(app_module.app):
        assert len(app_module._background_tasks) == 2
        assert cleanup_constructions == 0

    assert app_module._background_tasks == []
