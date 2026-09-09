"""P0 RAGFlow document-run terminal webhook: signature, idempotency, emit-on-change, ignore CANCEL."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.gateway.callback import sign_payload
from enterprise.gateway.config import GatewayConfig
from enterprise.gateway.sync.models import ExtDocumentMap, insert_mapping, utc_now
from enterprise.gateway.sync.ragflow_status_webhook import assert_intranet_client
from enterprise.gateway.sync.status_mapping import map_ragflow_run_to_sync_status

SECRET = "rf-status-webhook-test-secret"
EVENT_TYPE = "ragflow.document.run.terminal"


def _load_rf_webhook():
    root = Path(__file__).resolve().parents[2]
    path = root / "ragflow" / "api" / "utils" / "enterprise_status_webhook.py"
    spec = importlib.util.spec_from_file_location("enterprise_status_webhook_rf", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rf_wh = _load_rf_webhook()


def _envelope(*, event_id: str | None = None, run: str = "DONE", run_code: str = "3") -> dict:
    eid = event_id or rf_wh.stable_event_id("doc-1", run_code)
    return {
        "schemaVersion": "rf.status.v1",
        "eventId": eid,
        "eventType": EVENT_TYPE,
        "occurredAt": "2026-09-04T02:00:00.000Z",
        "payload": {
            "ragflowDocumentId": "doc-1",
            "ragflowDatasetId": "ds-1",
            "run": run,
            "runCode": run_code,
            "trigger": "sync_progress",
        },
    }


def _signed_headers(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    return {
        "Content-Type": "application/json",
        "X-Enterprise-Timestamp": str(ts),
        "X-Enterprise-Signature": sign_payload(body, SECRET, ts),
        "X-Enterprise-Event-Id": json.loads(body.decode())["eventId"],
    }


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_IGNORE_CANCEL", "true")
    monkeypatch.setenv(
        "ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_TRUSTED_CIDRS",
        "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0.0/16".replace(
            "192.168.0.0.0/16", "192.168.0.0/16"
        ),
    )
    import enterprise.gateway.config as config_module

    config_module.config = GatewayConfig()
    yield config_module.config


def test_stable_event_id_ignores_update_time_jitter():
    a = rf_wh.stable_event_id("doc-a", "3", update_time=100)
    b = rf_wh.stable_event_id("doc-a", "3", update_time=999)
    assert a == b
    assert a != rf_wh.stable_event_id("doc-a", "4")


def test_emit_only_on_run_value_change(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_STATUS_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_STATUS_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    monkeypatch.setenv("ENTERPRISE_STATUS_WEBHOOK_SECRET", SECRET)
    assert rf_wh.runs_differ("RUNNING", "DONE")
    assert not rf_wh.runs_differ("DONE", "DONE")
    assert not rf_wh.runs_differ("3", "DONE")
    assert (
        rf_wh.emit_document_run_terminal(
            doc_id="d1",
            kb_id="k1",
            old_run="DONE",
            new_run="DONE",
            trigger="sync_progress",
            background=False,
        )
        is False
    )
    assert (
        rf_wh.emit_document_run_terminal(
            doc_id="d1",
            kb_id="k1",
            old_run="RUNNING",
            new_run="DONE",
            trigger="sync_progress",
            background=True,
        )
        is True
    )


def test_build_envelope_terminal_only():
    assert rf_wh.build_terminal_envelope(doc_id="d", kb_id="k", run="RUNNING") is None
    env = rf_wh.build_terminal_envelope(doc_id="d", kb_id="k", run="FAIL", trigger="task_fail")
    assert env is not None
    assert env["eventType"] == EVENT_TYPE
    assert env["payload"]["run"] == "FAIL"
    assert env["payload"]["runCode"] == "4"


@pytest.mark.asyncio
async def test_signature_rejection(webhook_env, isolated_gateway_db):
    from enterprise.gateway.app import app

    body = json.dumps(_envelope()).encode("utf-8")
    headers = _signed_headers(body)
    headers["X-Enterprise-Signature"] = "sha256=" + ("ab" * 32)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/enterprise/api/v1/internal/ragflow/document-run-terminal",
            content=body,
            headers=headers,
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_idempotent_inbox_no_double_apply(webhook_env, isolated_gateway_db, monkeypatch):
    from enterprise.gateway.app import app

    gateway, _ = isolated_gateway_db
    now = utc_now()
    doc = ExtDocumentMap(
        tenant_id="t1",
        source_system="EAM",
        external_document_id="EXT-1",
        source_version_id="v1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name="manual.pdf",
        sync_status="parsing",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-1",
        processing_round=1,
        created_at=now,
        updated_at=now,
    )
    async with gateway.transaction(write=True) as conn:
        await insert_mapping(conn, doc)

    calls: list[tuple[str, str]] = []

    async def _fake_apply(self, mapping, run, *, source="poll"):
        calls.append((mapping.ragflow_document_id or "", str(run)))
        return mapping

    monkeypatch.setattr(
        "enterprise.gateway.sync.sync_service.SyncService.apply_ragflow_run",
        _fake_apply,
        raising=True,
    )

    body = json.dumps(_envelope()).encode("utf-8")
    headers = _signed_headers(body)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/enterprise/api/v1/internal/ragflow/document-run-terminal",
            content=body,
            headers=headers,
        )
        r2 = await client.post(
            "/enterprise/api/v1/internal/ragflow/document-run-terminal",
            content=body,
            headers=headers,
        )
    assert r1.status_code == 200
    assert r1.json().get("accepted") is True
    assert r1.json().get("replay") is False
    assert r2.status_code == 200
    assert r2.json().get("replay") is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ignore_cancel_by_default(webhook_env, isolated_gateway_db, monkeypatch):
    from enterprise.gateway.app import app

    calls: list[str] = []

    async def _fake_apply(self, mapping, run, *, source="poll"):
        calls.append(str(run))
        return mapping

    monkeypatch.setattr(
        "enterprise.gateway.sync.sync_service.SyncService.apply_ragflow_run",
        _fake_apply,
        raising=True,
    )
    body = json.dumps(_envelope(run="CANCEL", run_code="2")).encode("utf-8")
    headers = _signed_headers(body)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/enterprise/api/v1/internal/ragflow/document-run-terminal",
            content=body,
            headers=headers,
        )
    assert resp.status_code == 200
    assert resp.json().get("ignored") == "cancel"
    assert calls == []


def test_map_cancel_still_defined():
    assert map_ragflow_run_to_sync_status("CANCEL") == "cancelled"
    assert map_ragflow_run_to_sync_status("2") == "cancelled"


def test_intranet_reject_public_ip(webhook_env):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "8.8.8.8"
    req.headers = {}
    with pytest.raises(PermissionError):
        assert_intranet_client(req)


def test_intranet_allow_private_ip(webhook_env):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "10.1.2.3"
    req.headers = {}
    assert_intranet_client(req)
