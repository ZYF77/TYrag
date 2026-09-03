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
    legacy = _settings_payload()
    legacy.pop("diagnostics")
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
    async with gateway_db.transaction() as conn:
        row = await fetchone(
            conn,
            "SELECT settings_json FROM gateway_runtime_settings WHERE id=?",
            (1,),
        )
    assert json.loads(row["settings_json"])["diagnostics"] == {"enabled": False}
    config.clear_runtime_settings()


def test_runtime_limits_are_read_by_source_adapters_without_restart():
    settings = parse_runtime_settings(_settings_payload())
    config.apply_runtime_settings(settings)
    try:
        assert FileShareSourceAdapter(roots={"root": "."}).max_size_bytes == 96 * 1024 * 1024
        assert S3SourceAdapter(max_size_bytes=None).max_size_bytes == 80 * 1024 * 1024
        assert attachment_max_size_bytes() == 8 * 1024 * 1024
        assert config.rag_diagnostics_enabled is True
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
