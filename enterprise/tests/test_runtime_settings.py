"""Runtime settings validation and PostgreSQL persistence tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from enterprise.gateway.config import config
from enterprise.gateway.db.dialect import exec_sql, fetchone
from enterprise.gateway.runtime_settings import (
    RuntimeSettingsError,
    RuntimeSettingsManager,
    parse_runtime_settings,
)
from enterprise.gateway.sync.external_source import FileShareSourceAdapter
from enterprise.gateway.sync.source_adapter import S3SourceAdapter
from enterprise.gateway.sync.transient_attachment import attachment_max_size_bytes
from enterprise.gateway.sync.worker import OutboxWorker


def _settings_payload() -> dict:
    return {
        "outbox": {"enabled": True, "pollSeconds": 5.0},
        "statusReconciler": {"enabled": False, "pollSeconds": 12.0},
        "transientAttachmentCleanup": {
            "enabled": True,
            "pollSeconds": 30.0,
            "ttlSeconds": 7200,
        },
        "qualityEvaluation": {"enabled": False, "pollSeconds": 4.0},
        "qualityReconciler": {
            "enabled": True,
            "pollSeconds": 15.0,
            "runningTimeoutSeconds": 3600,
        },
        "callbackDelivery": {"enabled": True, "pollSeconds": 3.0},
        "limits": {
            "fileShareMaxMiB": 96,
            "s3MaxMiB": 80,
            "transientAttachmentMaxMiB": 8,
        },
        "diagnostics": {"enabled": True},
        "retrievalScope": {"policy": "legacy_device"},
        "ragflowStatusWebhook": {
            "enabled": False,
            "secretConfigured": False,
            "ignoreCancel": True,
        },
        "userMemory": {
            "enabled": False,
            "memoryId": "",
            "topN": 5,
            "timeoutSeconds": 5.0,
        },
        "workflow": {
            "enabled": False,
            "agentId": "",
            "version": "",
            "timeoutSeconds": 120.0,
        },
    }


def test_runtime_settings_parser_rejects_unknown_and_unsafe_values():
    payload = _settings_payload()
    payload["unexpected"] = True
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)

    payload = _settings_payload()
    payload["limits"]["fileShareMaxMiB"] = 129
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)


def test_transient_attachment_environment_limit_is_hard_capped(monkeypatch):
    config.clear_runtime_settings()
    monkeypatch.delenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES", raising=False)
    monkeypatch.setenv("ENTERPRISE_ATTACHMENT_MAX_SIZE_MB", "512")
    assert attachment_max_size_bytes() == 10 * 1024 * 1024

    payload = _settings_payload()
    payload["transientAttachmentCleanup"]["ttlSeconds"] = 59
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)


@pytest.mark.asyncio
async def test_runtime_settings_manager_bootstraps_and_persists(gateway_db):
    config.clear_runtime_settings()
    manager = RuntimeSettingsManager(gateway_db)
    await manager.ensure_loaded()
    assert manager.response()["source"] == "environment"
    assert manager.snapshot().file_share_max_size_mb == 128

    settings = parse_runtime_settings(_settings_payload())
    await manager.update(settings, updated_by="admin-user")
    assert manager.response()["source"] == "database"
    assert manager.snapshot() == settings

    config.clear_runtime_settings()
    reloaded = RuntimeSettingsManager(gateway_db)
    await reloaded.ensure_loaded()
    assert reloaded.response()["source"] == "database"
    assert reloaded.snapshot() == settings
    config.clear_runtime_settings()
@pytest.mark.asyncio
async def test_runtime_settings_manager_backfills_diagnostics_for_existing_row(gateway_db, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_RAG_DIAGNOSTICS_ENABLED", "false")
    monkeypatch.delenv("ENTERPRISE_RETRIEVAL_SCOPE_POLICY", raising=False)
    legacy = _settings_payload()
    legacy.pop("diagnostics")
    legacy.pop("retrievalScope")
    legacy.pop("ragflowStatusWebhook")
    legacy.pop("userMemory")
    legacy.pop("workflow")
    async with gateway_db.transaction(write=True) as conn:
        await exec_sql(
            conn,
            "INSERT INTO gateway_runtime_settings "
            "(id, settings_json, updated_at, updated_by) VALUES (?, ?, ?, NULL)",
            (1, json.dumps(legacy), "2026-09-01T00:00:00+00:00"),
        )

    config.clear_runtime_settings()
    manager = RuntimeSettingsManager(gateway_db)
    await manager.ensure_loaded()

    assert manager.snapshot().rag_diagnostics_enabled is False
    assert manager.snapshot().retrieval_scope_policy == "legacy_device"
    async with gateway_db.transaction() as conn:
        row = await fetchone(
            conn,
            "SELECT settings_json FROM gateway_runtime_settings WHERE id=?",
            (1,),
        )
    stored = json.loads(row["settings_json"])
    assert stored["diagnostics"] == {"enabled": False}
    assert stored["retrievalScope"] == {"policy": "legacy_device"}
    assert stored["ragflowStatusWebhook"]["enabled"] is False
    assert stored["ragflowStatusWebhook"]["ignoreCancel"] is True
    assert "secret" in stored["ragflowStatusWebhook"]
    assert stored["userMemory"] == {
        "enabled": False,
        "memoryId": "",
        "topN": 5,
        "timeoutSeconds": 5.0,
    }
    assert stored["workflow"] == {
        "enabled": False,
        "agentId": "",
        "version": "",
        "timeoutSeconds": 120.0,
    }
    api_webhook = manager.snapshot().to_api()["ragflowStatusWebhook"]
    assert api_webhook == {
        "enabled": False,
        "secretConfigured": False,
        "ignoreCancel": True,
    }
    assert "secret" not in api_webhook
    config.clear_runtime_settings()

def test_runtime_limits_are_read_by_source_adapters_without_restart():
    settings = parse_runtime_settings(_settings_payload())
    config.apply_runtime_settings(settings)
    try:
        assert FileShareSourceAdapter(roots={"root": "."}).max_size_bytes == 96 * 1024 * 1024
        assert S3SourceAdapter(max_size_bytes=None).max_size_bytes == 80 * 1024 * 1024
        assert attachment_max_size_bytes() == 8 * 1024 * 1024
        assert config.rag_diagnostics_enabled is True
        assert config.retrieval_scope_policy == "legacy_device"
    finally:
        config.clear_runtime_settings()


@pytest.mark.asyncio
async def test_outbox_loop_reads_enabled_and_interval_each_cycle(monkeypatch):
    settings = parse_runtime_settings(_settings_payload())
    config.apply_runtime_settings(settings)
    worker = OutboxWorker(object())
    runs = 0
    sleeps: list[float] = []

    async def run_once():
        nonlocal runs
        runs += 1
        config.apply_runtime_settings(
            replace(settings, outbox_enabled=False, outbox_poll_seconds=7.0)
        )
        return 1

    async def sleep(interval: float):
        sleeps.append(interval)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr("enterprise.gateway.sync.worker.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_forever()

    assert runs == 1
    assert sleeps == [7.0]
    config.clear_runtime_settings()


def test_runtime_retrieval_scope_policy_falls_back_and_applies_hot():
    payload = _settings_payload()
    payload["retrievalScope"]["policy"] = "authorized_context"
    settings = parse_runtime_settings(payload)
    assert settings.retrieval_scope_policy == "authorized_context"

    payload = _settings_payload()
    payload["retrievalScope"]["policy"] = "unknown_policy"
    settings = parse_runtime_settings(payload)
    assert settings.retrieval_scope_policy == "legacy_device"

    config.clear_runtime_settings()
    try:
        config.apply_runtime_settings(
            parse_runtime_settings(
                {
                    **_settings_payload(),
                    "retrievalScope": {"policy": "authorized_context"},
                }
            )
        )
        assert config.retrieval_scope_policy == "authorized_context"
        from enterprise.gateway.query import v2_router

        assert v2_router._current_retrieval_scope_policy() == "authorized_context"
    finally:
        config.clear_runtime_settings()
        assert config.retrieval_scope_policy == "legacy_device"


def test_ragflow_status_webhook_secret_write_only_and_hot_toggle():
    payload = _settings_payload()
    payload["ragflowStatusWebhook"] = {
        "enabled": True,
        "secret": "super-secret",
        "ignoreCancel": False,
    }
    settings = parse_runtime_settings(payload)
    assert settings.ragflow_status_webhook_enabled is True
    assert settings.ragflow_status_webhook_secret == "super-secret"
    assert settings.ragflow_status_webhook_ignore_cancel is False
    public = settings.to_api()["ragflowStatusWebhook"]
    assert public == {
        "enabled": True,
        "secretConfigured": True,
        "ignoreCancel": False,
    }
    assert "secret" not in public

    # Empty secret on update keeps previous
    kept = parse_runtime_settings(
        {
            **_settings_payload(),
            "ragflowStatusWebhook": {
                "enabled": False,
                "secret": "",
                "ignoreCancel": True,
            },
        },
        previous_secret="super-secret",
    )
    assert kept.ragflow_status_webhook_enabled is False
    assert kept.ragflow_status_webhook_secret == "super-secret"

    config.clear_runtime_settings()
    try:
        config.apply_runtime_settings(settings)
        assert config.ragflow_status_webhook_enabled is True
        assert config.ragflow_status_webhook_secret == "super-secret"
        assert config.runtime_settings().ragflow_status_webhook_enabled is True
        config.apply_runtime_settings(kept)
        assert config.ragflow_status_webhook_enabled is False
        assert config.ragflow_status_webhook_secret == "super-secret"
    finally:
        config.clear_runtime_settings()


@pytest.mark.asyncio
async def test_runtime_settings_persists_webhook_secret_without_api_echo(gateway_db):
    config.clear_runtime_settings()
    manager = RuntimeSettingsManager(gateway_db)
    await manager.ensure_loaded()
    payload = _settings_payload()
    payload["ragflowStatusWebhook"] = {
        "enabled": True,
        "secret": "persist-me",
        "ignoreCancel": True,
    }
    await manager.update(parse_runtime_settings(payload), updated_by="admin")
    response = manager.response()
    assert response["settings"]["ragflowStatusWebhook"] == {
        "enabled": True,
        "secretConfigured": True,
        "ignoreCancel": True,
    }
    assert "secret" not in response["settings"]["ragflowStatusWebhook"]

    config.clear_runtime_settings()
    reloaded = RuntimeSettingsManager(gateway_db)
    await reloaded.ensure_loaded()
    assert reloaded.snapshot().ragflow_status_webhook_secret == "persist-me"
    assert reloaded.snapshot().ragflow_status_webhook_enabled is True
    config.clear_runtime_settings()


def test_user_memory_runtime_parse_and_hot_toggle():
    payload = _settings_payload()
    payload["userMemory"] = {
        "enabled": True,
        "memoryId": "mem-hot-1",
        "topN": 8,
        "timeoutSeconds": 7.5,
    }
    settings = parse_runtime_settings(payload)
    assert settings.user_memory_enabled is True
    assert settings.user_memory_id == "mem-hot-1"
    assert settings.user_memory_top_n == 8
    assert settings.user_memory_timeout_seconds == 7.5
    public = settings.to_api()["userMemory"]
    assert public == {
        "enabled": True,
        "memoryId": "mem-hot-1",
        "topN": 8,
        "timeoutSeconds": 7.5,
    }

    # enabled=true + empty memoryId is allowed to save (runtime no-op)
    empty_id = parse_runtime_settings(
        {
            **_settings_payload(),
            "userMemory": {
                "enabled": True,
                "memoryId": "",
                "topN": 5,
                "timeoutSeconds": 5.0,
            },
        }
    )
    assert empty_id.user_memory_enabled is True
    assert empty_id.user_memory_id == ""

    payload = _settings_payload()
    payload["userMemory"]["topN"] = 21
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)

    payload = _settings_payload()
    payload["userMemory"]["timeoutSeconds"] = 0.1
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)

    config.clear_runtime_settings()
    try:
        off = parse_runtime_settings(_settings_payload())
        config.apply_runtime_settings(off)
        assert config.user_memory_enabled is False
        assert config.runtime_settings().user_memory_enabled is False

        on = parse_runtime_settings(
            {
                **_settings_payload(),
                "userMemory": {
                    "enabled": True,
                    "memoryId": "mem-hot-1",
                    "topN": 3,
                    "timeoutSeconds": 4.0,
                },
            }
        )
        config.apply_runtime_settings(on)
        assert config.user_memory_enabled is True
        assert config.enterprise_memory_id == "mem-hot-1"
        assert config.user_memory_top_n == 3
        assert config.user_memory_timeout == 4.0
        assert config.runtime_settings().user_memory_id == "mem-hot-1"
    finally:
        config.clear_runtime_settings()


@pytest.mark.asyncio
async def test_user_memory_section_backfills_from_env(gateway_db, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_USER_MEMORY_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_MEMORY_ID", "mem-from-env")
    monkeypatch.setenv("ENTERPRISE_USER_MEMORY_TOP_N", "9")
    monkeypatch.setenv("ENTERPRISE_USER_MEMORY_TIMEOUT", "6.5")
    legacy = _settings_payload()
    legacy.pop("userMemory")
    async with gateway_db.transaction(write=True) as conn:
        await exec_sql(
            conn,
            "INSERT INTO gateway_runtime_settings "
            "(id, settings_json, updated_at, updated_by) VALUES (?, ?, ?, NULL)",
            (1, json.dumps(legacy), "2026-09-01T00:00:00+00:00"),
        )

    config.clear_runtime_settings()
    manager = RuntimeSettingsManager(gateway_db)
    await manager.ensure_loaded()
    snap = manager.snapshot()
    assert snap.user_memory_enabled is True
    assert snap.user_memory_id == "mem-from-env"
    assert snap.user_memory_top_n == 9
    assert snap.user_memory_timeout_seconds == 6.5
    async with gateway_db.transaction() as conn:
        row = await fetchone(
            conn,
            "SELECT settings_json FROM gateway_runtime_settings WHERE id=?",
            (1,),
        )
    stored = json.loads(row["settings_json"])
    assert stored["userMemory"]["memoryId"] == "mem-from-env"
    config.clear_runtime_settings()


def test_workflow_runtime_parse_and_hot_toggle():
    payload = _settings_payload()
    payload["workflow"] = {
        "enabled": True,
        "agentId": "9d6f54beb0b911f1ad1c8d8c8b7d5b0e",
        "version": "enterprise-qa-agent-v1.2",
        "timeoutSeconds": 90.0,
    }
    settings = parse_runtime_settings(payload)
    assert settings.workflow_enabled is True
    assert settings.workflow_agent_id == "9d6f54beb0b911f1ad1c8d8c8b7d5b0e"
    assert settings.workflow_version == "enterprise-qa-agent-v1.2"
    assert settings.workflow_timeout_seconds == 90.0
    public = settings.to_api()["workflow"]
    assert public == {
        "enabled": True,
        "agentId": "9d6f54beb0b911f1ad1c8d8c8b7d5b0e",
        "version": "enterprise-qa-agent-v1.2",
        "timeoutSeconds": 90.0,
    }

    # enabled=true + empty agentId/version is allowed to save; router returns NOT_CONFIGURED
    empty = parse_runtime_settings(
        {
            **_settings_payload(),
            "workflow": {
                "enabled": True,
                "agentId": "",
                "version": "",
                "timeoutSeconds": 120.0,
            },
        }
    )
    assert empty.workflow_enabled is True
    assert empty.workflow_agent_id == ""
    assert empty.workflow_version == ""

    payload = _settings_payload()
    payload["workflow"]["timeoutSeconds"] = 0.5
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)

    payload = _settings_payload()
    payload["workflow"]["timeoutSeconds"] = 601.0
    with pytest.raises(RuntimeSettingsError):
        parse_runtime_settings(payload)

    config.clear_runtime_settings()
    try:
        off = parse_runtime_settings(_settings_payload())
        config.apply_runtime_settings(off)
        assert config.workflow_enabled is False
        assert config.runtime_settings().workflow_enabled is False

        on = parse_runtime_settings(
            {
                **_settings_payload(),
                "workflow": {
                    "enabled": True,
                    "agentId": "agent-hot-1",
                    "version": "enterprise-qa-agent-v1.2",
                    "timeoutSeconds": 88.0,
                },
            }
        )
        config.apply_runtime_settings(on)
        assert config.workflow_enabled is True
        assert config.workflow_agent_id == "agent-hot-1"
        assert config.workflow_version == "enterprise-qa-agent-v1.2"
        assert config.workflow_timeout == 88.0
        assert config.runtime_settings().workflow_agent_id == "agent-hot-1"
    finally:
        config.clear_runtime_settings()


@pytest.mark.asyncio
async def test_workflow_section_backfills_from_env(gateway_db, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_WORKFLOW_AGENT_ID", "agent-from-env")
    monkeypatch.setenv("ENTERPRISE_WORKFLOW_VERSION", "enterprise-qa-agent-v1.2")
    monkeypatch.setenv("ENTERPRISE_WORKFLOW_TIMEOUT", "95")
    legacy = _settings_payload()
    legacy.pop("workflow")
    async with gateway_db.transaction(write=True) as conn:
        await exec_sql(
            conn,
            "INSERT INTO gateway_runtime_settings "
            "(id, settings_json, updated_at, updated_by) VALUES (?, ?, ?, NULL)",
            (1, json.dumps(legacy), "2026-09-01T00:00:00+00:00"),
        )

    config.clear_runtime_settings()
    manager = RuntimeSettingsManager(gateway_db)
    await manager.ensure_loaded()
    snap = manager.snapshot()
    assert snap.workflow_enabled is True
    assert snap.workflow_agent_id == "agent-from-env"
    assert snap.workflow_version == "enterprise-qa-agent-v1.2"
    assert snap.workflow_timeout_seconds == 95.0
    async with gateway_db.transaction() as conn:
        row = await fetchone(
            conn,
            "SELECT settings_json FROM gateway_runtime_settings WHERE id=?",
            (1,),
        )
    stored = json.loads(row["settings_json"])
    assert stored["workflow"]["agentId"] == "agent-from-env"
    config.clear_runtime_settings()
