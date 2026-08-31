"""Real local HTTP acceptance for private RAG diagnostics.

Run inside the Gateway container so credentials stay in its environment.  The
script prints metadata-only results and never prints tokens, answers, prompts,
chunk bodies, or raw model responses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid

import httpx
import jwt

from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.dialect import fetchone


BASE = "http://127.0.0.1:5188/enterprise/api"


async def _fixture() -> dict:
    gateway = GatewayDatabase.from_env()
    try:
        async with gateway.transaction(write=False) as conn:
            row = await fetchone(
                conn,
                """SELECT tenant_id, equipment_id, fixed_asset_no,
                          department_id, security_level, allow_group_ids
                     FROM ext_document_map
                    WHERE ragflow_document_id IS NOT NULL
                      AND sync_status='ready' AND business_status='active'
                      AND (
                        tenant_id=? OR lower(tenant_id) LIKE '%e2e%'
                        OR lower(tenant_id) LIKE '%test%'
                        OR lower(tenant_id) LIKE '%demo%'
                        OR lower(tenant_id) LIKE '%integration%'
                        OR lower(source_system) IN ('e2e', 'test', 'demo')
                      )
                    ORDER BY CASE WHEN tenant_id=? THEN 0 ELSE 1 END,
                             updated_at DESC LIMIT 1""",
                (
                    os.getenv("ENTERPRISE_E2E_TENANT_ID", "tyrag-integration"),
                    os.getenv("ENTERPRISE_E2E_TENANT_ID", "tyrag-integration"),
                ),
            )
        if row is None:
            raise RuntimeError("non_sensitive_ready_fixture_missing")
        return dict(row)
    finally:
        await gateway.dispose()


def _token(fixture: dict) -> str:
    now = int(time.time())
    groups = json.loads(fixture.get("allow_group_ids") or "[]")
    payload = {
        "sub": "rag-diagnostics-e2e",
        "business_user_id": "rag-diagnostics-e2e",
        "tenant": fixture["tenant_id"],
        "name": "RAG Diagnostics E2E",
        "roles": ["system_admin", "end_user"],
        "department": [fixture.get("department_id") or "maintenance"],
        "groups": groups or ["maintenance"],
        "security_level": int(fixture.get("security_level") or 0),
        "iss": os.environ["JWT_ISSUER"],
        "aud": os.environ["JWT_AUDIENCE"],
        "iat": now - 5,
        "exp": now + 900,
    }
    return jwt.encode(payload, os.environ["JWT_SHARED_SECRET"], algorithm="HS256")


def _events(trace: dict) -> set[str]:
    return {
        str(event.get("type"))
        for event in trace.get("diagnostics", {}).get("events", [])
        if isinstance(event, dict)
    }


def _sse_run_id(text: str) -> str:
    for block in text.split("\n\n"):
        lines = block.splitlines()
        data = next((line[5:].strip() for line in lines if line.startswith("data:")), "")
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("runId"):
            return str(payload["runId"])
    raise RuntimeError("sse_run_id_missing")


def main(*, base_url: str = BASE, expect_upstream_failure: bool = False) -> int:
    base_url = base_url.rstrip("/")
    fixture = asyncio.run(_fixture())
    headers = {"Authorization": f"Bearer {_token(fixture)}"}
    context = {}
    if fixture.get("equipment_id"):
        context["equipmentId"] = fixture["equipment_id"]
    elif fixture.get("fixed_asset_no"):
        context["fixedAssetNo"] = fixture["fixed_asset_no"]

    required = {"request", "scope", "retrieval", "context", "llm", "outcome"}
    with httpx.Client(timeout=180) as client:
        created = client.post(f"{base_url}/v2/conversations", headers=headers, json=context)
        created.raise_for_status()
        conversation_id = created.json()["conversationId"]

        if expect_upstream_failure:
            client_message_id = f"diag-fail-{uuid.uuid4().hex[:12]}"
            failed = client.post(
                f"{base_url}/v2/conversations/{conversation_id}/messages",
                headers=headers,
                json={
                    "clientMessageId": client_message_id,
                    "question": "验证上游不可用时的诊断旁路。",
                },
            )
            if failed.status_code != 503 or failed.json().get("code") != "RAGFLOW_UNAVAILABLE":
                raise RuntimeError("upstream_failure_semantics_changed")
            if "_diagnostics" in failed.text:
                raise RuntimeError("public_failure_leaked_diagnostics")
            listed = client.get(
                f"{base_url}/v1/admin/system/diagnostics/traces?limit=50",
                headers=headers,
            )
            listed.raise_for_status()
            item = next(
                (
                    row
                    for row in listed.json().get("items", [])
                    if row.get("clientMessageId") == client_message_id
                ),
                None,
            )
            if not item:
                raise RuntimeError("partial_failure_trace_missing")
            detail = client.get(
                f"{base_url}/v1/admin/system/diagnostics/traces/{item['runId']}",
                headers=headers,
            )
            detail.raise_for_status()
            events = _events(detail.json())
            if not {"request", "outcome"}.issubset(events):
                raise RuntimeError("partial_failure_trace_incomplete")
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "failure": {
                            "httpStatus": failed.status_code,
                            "code": failed.json().get("code"),
                            "events": sorted(events),
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        normal = client.post(
            f"{base_url}/v2/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "clientMessageId": f"diag-json-{uuid.uuid4().hex[:12]}",
                "question": "请基于当前测试文档给出一条简短的维护建议。",
            },
        )
        normal.raise_for_status()
        normal_body = normal.json()
        if "_diagnostics" in normal_body:
            raise RuntimeError("public_json_leaked_diagnostics")
        normal_trace = client.get(
            f"{base_url}/v1/admin/system/diagnostics/traces/{normal_body['runId']}",
            headers=headers,
        )
        normal_trace.raise_for_status()
        normal_events = _events(normal_trace.json())
        if not required.issubset(normal_events):
            raise RuntimeError("json_trace_stages_incomplete")

        streamed = client.post(
            f"{base_url}/v2/conversations/{conversation_id}/messages",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "clientMessageId": f"diag-sse-{uuid.uuid4().hex[:12]}",
                "question": "请进一步说明维护时的注意事项。",
                "reasoningMode": "high",
            },
        )
        streamed.raise_for_status()
        if "_diagnostics" in streamed.text:
            raise RuntimeError("public_sse_leaked_diagnostics")
        stream_run_id = _sse_run_id(streamed.text)
        stream_trace = client.get(
            f"{base_url}/v1/admin/system/diagnostics/traces/{stream_run_id}",
            headers=headers,
        )
        stream_trace.raise_for_status()
        stream_events = _events(stream_trace.json())
        if not required.issubset(stream_events):
            raise RuntimeError("sse_trace_stages_incomplete")

    print(
        json.dumps(
            {
                "status": "PASS",
                "json": {"status": normal_body.get("status"), "events": sorted(normal_events)},
                "sse": {"completed": "event: answer.completed" in streamed.text, "events": sorted(stream_events)},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("GATEWAY_URL", BASE))
    parser.add_argument("--expect-upstream-failure", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        main(base_url=args.base_url, expect_upstream_failure=args.expect_upstream_failure)
    )
