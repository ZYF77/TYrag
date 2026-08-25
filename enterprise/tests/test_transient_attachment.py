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
    remember_ragflow_temp_file,
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
async def test_explicit_disable_returns_stable_unavailable_before_dependencies(
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
        create = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json={
                "fileName": "manual.pdf",
                "mediaType": "application/pdf",
                "content": base64.b64encode(b"disabled").decode("ascii"),
            },
        )
        ticket = await client.post(
            "/enterprise/api/v2/attachments/attachment-a/ticket"
        )
        download = await client.get(
            "/enterprise/api/v2/attachments/attachment-a/download/ticket-a"
        )

    for response in (create, ticket, download):
        assert response.status_code == 503
        assert response.json()["code"] == "ATTACHMENT_STORAGE_UNAVAILABLE"
        assert response.json()["retryable"] is True


@pytest.mark.asyncio
async def test_attachment_post_is_reachable_by_default(storage_env, monkeypatch):
    assert config.transient_attachments_enabled is True
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
    assert response.json()["maxDownloads"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_formal_attachment_jwt_errors_use_error_envelope():
    application = FastAPI()
    application.include_router(router)
    application.add_exception_handler(
        app_module.UserAuthError, app_module.user_auth_error_handler
    )
    application.dependency_overrides[get_db] = lambda: None
    application.dependency_overrides[get_storage] = lambda: None

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://gateway.test"
    ) as client:
        create = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json={
                "fileName": "manual.pdf",
                "mediaType": "application/pdf",
                "content": base64.b64encode(b"jwt-required").decode("ascii"),
            },
        )
        ticket = await client.post(
            "/enterprise/api/v2/attachments/attachment-a/ticket"
        )

    for response in (create, ticket):
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TOKEN_MISSING"
        assert response.json()["retryable"] is False


def test_formal_attachment_routes_are_visible_in_openapi():
    application = FastAPI()
    application.include_router(router)
    paths = application.openapi()["paths"]

    assert "/enterprise/api/v2/conversations/{conversation_id}/attachments" in paths
    assert "/enterprise/api/v2/attachments/{attachment_id}/ticket" in paths
    assert "/enterprise/api/v2/attachments/{attachment_id}/download/{ticket}" in paths
    assert "post" in paths[
        "/enterprise/api/v2/conversations/{conversation_id}/attachments"
    ]
    assert paths[
        "/enterprise/api/v2/conversations/{conversation_id}/attachments"
    ]["post"]["operationId"] == "v2CreateConversationAttachment"
    assert paths[
        "/enterprise/api/v2/attachments/{attachment_id}/ticket"
    ]["post"]["operationId"] == "v2IssueAttachmentTicket"
    assert paths[
        "/enterprise/api/v2/attachments/{attachment_id}/download/{ticket}"
    ]["get"]["operationId"] == "v2DownloadAttachment"


@pytest.mark.asyncio
async def test_create_rejects_missing_and_archived_conversation_with_error_envelope(
    storage_env,
):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_storage] = lambda: storage
    application.dependency_overrides[require_user_principal] = lambda: owner

    payload = {
        "fileName": "manual.pdf",
        "mediaType": "application/pdf",
        "content": base64.b64encode(b"envelope-bytes").decode("ascii"),
    }
    await _conversation(db, owner)
    await v2_store.archive_conversation(
        db,
        conversation_id="conversation-a",
        tenant_id=owner.tenant_id,
        business_user_id=owner.business_user_id,
    )

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://gateway.test"
    ) as client:
        missing = await client.post(
            "/enterprise/api/v2/conversations/missing/attachments", json=payload
        )
        archived = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json=payload,
        )

    assert missing.status_code == 404
    assert missing.json() == {
        "code": "CONVERSATION_NOT_FOUND",
        "message": "Conversation not found",
        "requestId": missing.json()["requestId"],
        "retryable": False,
    }
    assert archived.status_code == 409
    assert archived.json()["code"] == "CONVERSATION_ARCHIVED"
    assert archived.json()["retryable"] is False
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
async def test_upload_failure_is_retryable_and_scheduled_for_cleanup(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)
    storage.put_failures = 3
    service = TransientAttachmentService(db, storage)

    with pytest.raises(TransientAttachmentError) as unavailable:
        await service.create(
            tenant_id=owner.tenant_id,
            conversation_id="conversation-a",
            business_user_id=owner.business_user_id,
            file_name="manual.pdf",
            media_type="application/pdf",
            content=b"upload-failure",
        )

    assert unavailable.value.code == "ATTACHMENT_STORAGE_UNAVAILABLE"
    assert unavailable.value.status_code == 503
    assert unavailable.value.retryable is True
    conversation = await v2_store.get_conversation(
        db,
        conversation_id="conversation-a",
        tenant_id=owner.tenant_id,
        business_user_id=owner.business_user_id,
    )
    assert conversation["status"] == "active"

    async with db.execute(
        "SELECT status, next_retry_at FROM ext_transient_attachment"
    ) as cursor:
        pending = await cursor.fetchone()
    assert pending["status"] == "delete_retry"
    assert pending["next_retry_at"]

    cleaned = await service.cleanup_expired()
    assert cleaned["deleted"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_storage_integrity_failure_is_stable_and_ticket_can_retry(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    await _conversation(db, owner)
    service = TransientAttachmentService(db, storage)
    record, ticket = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="manual.pdf",
        media_type="application/pdf",
        content=b"integrity-bytes",
    )
    object_key = next(
        key for key in storage.objects if key[0] == "attachment-test-bucket"
    )
    storage.objects[object_key] = (b"tampered", "application/pdf")

    with pytest.raises(TransientAttachmentError) as corrupt:
        await service.download(
            attachment_id=record.attachment_id,
            token=ticket.token,
            principal=owner,
        )
    assert corrupt.value.code == "ATTACHMENT_STORAGE_CORRUPT"
    assert corrupt.value.status_code == 502
    assert corrupt.value.retryable is False

    storage.objects[object_key] = (b"integrity-bytes", "application/pdf")
    downloaded = await service.download(
        attachment_id=record.attachment_id,
        token=ticket.token,
        principal=owner,
    )
    assert downloaded.content == b"integrity-bytes"
    await db.close()


@pytest.mark.asyncio
async def test_ticket_is_tenant_bound_and_repeated_download_is_denied(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    service = TransientAttachmentService(db, storage)
    owner = _principal()
    other_tenant = _principal("tenant-b", "user-b")
    other_user = _principal("tenant-a", "user-b")
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
            principal=other_user,
        )
    assert forbidden.value.code == "ATTACHMENT_FORBIDDEN"

    with pytest.raises(TransientAttachmentError) as cross_tenant:
        await service.download(
            attachment_id=record.attachment_id,
            token=ticket.token,
            principal=other_tenant,
        )
    assert cross_tenant.value.code == "ATTACHMENT_FORBIDDEN"

    second_record, _ = await service.create(
        tenant_id=owner.tenant_id,
        conversation_id="conversation-a",
        business_user_id=owner.business_user_id,
        file_name="second.pdf",
        media_type="application/pdf",
        content=b"second-attachment",
    )
    with pytest.raises(TransientAttachmentError) as wrong_attachment:
        await service.download(
            attachment_id=second_record.attachment_id,
            token=ticket.token,
            principal=owner,
        )
    assert wrong_attachment.value.code == "ATTACHMENT_TICKET_INVALID"

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
async def test_cleanup_recovers_already_expired_rows(storage_env, monkeypatch):
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
        content=b"expired-row-bytes",
    )
    clock[0] += timedelta(seconds=2)
    await db.execute(
        "UPDATE ext_transient_attachment SET status='expired' WHERE attachment_id=?",
        (record.attachment_id,),
    )
    await db.commit()

    result = await service.cleanup_expired()

    assert result["deleted"] == 1
    assert storage.objects == {}
    await db.close()


@pytest.mark.asyncio
async def test_gateway_contract_validates_permissions_and_hides_storage_url(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    owner = _principal()
    other = _principal("tenant-b", "user-b")
    same_tenant_other_user = _principal("tenant-a", "user-b")
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
        object_key = next(
            key for key in storage.objects if key[0] == "attachment-test-bucket"
        )
        assert object_key[1].startswith("transient-attachments/")
        async with db.execute("SELECT COUNT(*) AS count FROM ext_document_map") as cursor:
            assert (await cursor.fetchone())["count"] == 0
        async with db.execute("SELECT COUNT(*) AS count FROM sync_outbox") as cursor:
            assert (await cursor.fetchone())["count"] == 0

        async with db.execute(
            "SELECT created_at, expires_at FROM ext_transient_attachment "
            "WHERE attachment_id=?",
            (body["attachmentId"],),
        ) as cursor:
            timestamps = await cursor.fetchone()
        assert (
            datetime.fromisoformat(timestamps["expires_at"])
            - datetime.fromisoformat(timestamps["created_at"])
            == timedelta(hours=24)
        )

        issued = await client.post(
            f"/enterprise/api/v2/attachments/{body['attachmentId']}/ticket"
        )
        assert issued.status_code == 200, issued.text
        issued_body = issued.json()
        assert issued_body["indexPolicy"] == "never"
        issued_url = issued_body["downloadUrl"]

        wrong_mime = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json={**payload, "mediaType": "text/plain"},
        )
        assert wrong_mime.status_code == 422
        assert wrong_mime.json()["code"] == "ATTACHMENT_MIME_NOT_ALLOWED"

        application.dependency_overrides[require_user_principal] = lambda: same_tenant_other_user
        same_user_create = await client.post(
            "/enterprise/api/v2/conversations/conversation-a/attachments",
            json=payload,
        )
        assert same_user_create.status_code == 404
        assert same_user_create.json()["code"] == "CONVERSATION_NOT_FOUND"
        same_user_ticket = await client.post(
            f"/enterprise/api/v2/attachments/{body['attachmentId']}/ticket"
        )
        assert same_user_ticket.status_code == 403
        assert same_user_ticket.json()["code"] == "ATTACHMENT_FORBIDDEN"

        application.dependency_overrides[optional_user_principal] = lambda: same_tenant_other_user
        unauthorized = await client.get(issued_url)
        assert unauthorized.status_code == 403
        assert unauthorized.json()["code"] == "ATTACHMENT_FORBIDDEN"

        application.dependency_overrides[optional_user_principal] = lambda: None
        downloaded = await client.get(issued_url)
        assert downloaded.status_code == 200
        assert downloaded.content == b"gateway-bytes"
        repeated = await client.get(issued_url)
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
async def test_explicit_disable_keeps_cleanup_worker_running(tmp_path, monkeypatch):
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

    class CountingCleanupWorker(IdleWorker):
        def __init__(self, *args, **kwargs):
            nonlocal cleanup_constructions
            cleanup_constructions += 1

    monkeypatch.setattr(app_module, "OutboxWorker", IdleWorker)
    monkeypatch.setattr(app_module, "StatusReconciler", IdleWorker)
    monkeypatch.setattr(
        app_module,
        "TransientAttachmentCleanupWorker",
        CountingCleanupWorker,
    )

    async with app_module.lifespan(app_module.app):
        assert len(app_module._background_tasks) == 3
        assert cleanup_constructions == 1

    assert app_module._background_tasks == []


@pytest.mark.asyncio
async def test_cleanup_retries_ragflow_file_delete(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    service = TransientAttachmentService(
        db, storage, now_fn=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc)
    )
    await _conversation(db, _principal())
    record, _ = await service.create(
        tenant_id="tenant-a",
        conversation_id="conversation-a",
        business_user_id="user-a",
        file_name="photo.png",
        media_type="image/png",
        content=b"png-bytes",
    )
    await service.set_ragflow_file(record.attachment_id, "rf-orphan")
    deleted: list[str] = []

    async def boom(file_id: str) -> None:
        deleted.append(file_id)
        raise RuntimeError("ragflow down")

    await db.execute(
        "UPDATE ext_transient_attachment SET expires_at=? WHERE attachment_id=?",
        ("2020-01-01T00:00:00+00:00", record.attachment_id),
    )
    await db.commit()
    await service.cleanup_expired(delete_ragflow_file=boom)
    assert deleted == ["rf-orphan"]
    async with db.execute(
        "SELECT ragflow_file_deleted_at FROM ext_transient_attachment WHERE attachment_id=?",
        (record.attachment_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ragflow_file_deleted_at"] is None

    async def ok(file_id: str) -> None:
        deleted.append(file_id)

    await service.cleanup_expired(delete_ragflow_file=ok)
    assert deleted[-1] == "rf-orphan"
    async with db.execute(
        "SELECT ragflow_file_deleted_at FROM ext_transient_attachment WHERE attachment_id=?",
        (record.attachment_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ragflow_file_deleted_at"]
    await db.close()


@pytest.mark.asyncio
async def test_cleanup_only_deletes_ttl_expired_ragflow_temp_files(monkeypatch):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_TTL_SECONDS", "60")
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    service = TransientAttachmentService(
        db, storage, now_fn=lambda: now
    )
    await remember_ragflow_temp_file(db, "rf-new")
    await remember_ragflow_temp_file(db, "rf-expired")
    await remember_ragflow_temp_file(db, "rf-failed")
    await db.executemany(
        "UPDATE ext_ragflow_temp_file SET created_at=? WHERE file_id=?",
        (
            (now.isoformat(), "rf-new"),
            ((now - timedelta(seconds=61)).isoformat(), "rf-expired"),
            ((now - timedelta(seconds=61)).isoformat(), "rf-failed"),
        ),
    )
    await db.commit()
    deleted: list[str] = []

    async def delete(file_id: str) -> None:
        deleted.append(file_id)
        if file_id == "rf-failed":
            raise RuntimeError("ragflow down")

    result = await service.cleanup_expired(delete_ragflow_file=delete)
    assert deleted == ["rf-expired", "rf-failed"]
    assert result["ragflowTempDeleted"] == 1
    assert result["ragflowTempFailed"] == 1
    async with db.execute(
        "SELECT file_id, deleted_at FROM ext_ragflow_temp_file ORDER BY file_id",
    ) as cursor:
        rows = {row["file_id"]: row["deleted_at"] for row in await cursor.fetchall()}
    assert rows["rf-new"] is None
    assert rows["rf-expired"]
    assert rows["rf-failed"] is None
    await db.close()
