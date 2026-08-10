"""Unit and Gateway contract tests for transient conversation attachments.

These tests exercise the Enterprise service boundary with an in-memory object
store. They are not Integration-profile evidence for MinIO, RAGFlow or M2.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query import v2_store
from enterprise.gateway.sync.models import init_db
from enterprise.gateway.sync.source_adapter import S3SourceAdapter, SourceFile
from enterprise.gateway.sync.transient_attachment import (
    TransientAttachmentError,
    TransientAttachmentService,
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
async def test_expired_ticket_and_fetch_retry(storage_env):
    db = await init_db(":memory:")
    storage = MemoryObjectStorage()
    clock = [datetime(2026, 8, 10, tzinfo=timezone.utc)]
    service = TransientAttachmentService(db, storage, now_fn=lambda: clock[0])
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
