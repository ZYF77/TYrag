#!/usr/bin/env python3
"""Offline smoke for v2 inquiry: chips -> suggestion ask -> follow-up -> history.

Uses the ASGI app + RAGFlow stub (no live EAM/RAGFlow). Intended for local/
CI checks of the Phase A/B inquiry DX path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _main() -> int:
    import os

    os.environ.setdefault("ENTERPRISE_TEST_MODE", "1")
    os.environ.setdefault("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
    os.environ.setdefault(
        "ENTERPRISE_GATEWAY_TEST_DATABASE_URL",
        "postgresql+asyncpg://tyrag_gateway_test:tyrag_gateway_test@127.0.0.1:55432/tyrag_gateway_test",
    )

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from enterprise.gateway.auth.middleware import require_user_principal
    from enterprise.gateway.auth.user_principal import UserPrincipal
    from enterprise.gateway.config import config
    from enterprise.gateway.db.ops import gw_write
    from enterprise.gateway.db.testing import create_gateway
    from enterprise.gateway.query import formal_router, v2_router
    from enterprise.gateway.query.ragflow_client import RAGFlowQueryStub
    from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping

    config.context_compress_enabled = True
    config.context_compress_turns = 4
    config.context_compress_keep_recent = 2

    gateway = await create_gateway(":memory:")
    await gw_write(
        gateway,
        insert_mapping,
        ExtDocumentMap(
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-SMOKE",
            source_version_id="v1",
            event_id=str(uuid.uuid4()),
            sha256=hashlib.sha256(b"DOC-SMOKE").hexdigest(),
            file_name="DOC-SMOKE.pdf",
            asset_id="FA-SMOKE",
            equipment_id="EQ-SMOKE",
            fixed_asset_no="FA-SMOKE",
            department_id="d10",
            security_level=2,
            allow_group_ids=json.dumps(["maintenance"]),
            deny_group_ids="[]",
            ragflow_dataset_id="ds-smoke",
            ragflow_document_id="doc-smoke",
            sync_status="ready",
            pipeline_status="DONE",
            business_status="active",
            current_version=1,
        ),
    )

    app = FastAPI()
    app.include_router(v2_router.router)
    app.dependency_overrides[v2_router.get_db] = lambda: gateway
    app.dependency_overrides[require_user_principal] = lambda: UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )
    stub = RAGFlowQueryStub()
    formal_router._query_stub = stub
    base = "/enterprise/api/v2"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                f"{base}/conversations",
                json={"equipmentId": "EQ-SMOKE"},
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["suggestions"], "create must return chips"
            conversation_id = body["conversationId"]
            chip = body["suggestions"][0]
            suggested = await client.post(
                f"{base}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": "smoke-suggestion",
                    "suggestionId": chip["suggestionId"],
                    "contextVersion": chip["contextVersion"],
                },
            )
            assert suggested.status_code == 200, suggested.text
            follow = await client.post(
                f"{base}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": "smoke-follow",
                    "question": "跟进：还有哪些维护要点？",
                },
            )
            assert follow.status_code == 200, follow.text
            for index in range(2):
                response = await client.post(
                    f"{base}/conversations/{conversation_id}/messages",
                    json={
                        "clientMessageId": f"smoke-extra-{index}",
                        "question": f"补充问题-{index}",
                    },
                )
                assert response.status_code == 200, response.text
            detail = await client.get(f"{base}/conversations/{conversation_id}")
            history = await client.get(
                f"{base}/conversations/{conversation_id}/messages?limit=50"
            )
            assert detail.status_code == history.status_code == 200
            assert detail.json()["contextCompacted"] is False
            assert len(history.json()["items"]) >= 6
            config.context_compress_enabled = False
            after = await client.post(
                f"{base}/conversations/{conversation_id}/messages",
                json={
                    "clientMessageId": "smoke-after-compress",
                    "question": "压缩后继续提问",
                },
            )
            assert after.status_code == 200, after.text
            assert (stub._last_completion_body or {}).get("question") == (
                "压缩后继续提问"
            )
        print("v2 inquiry smoke OK")
        return 0
    finally:
        formal_router._query_stub = None
        await gateway.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
