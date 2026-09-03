"""Persisted, hot-reloadable Gateway runtime settings."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from enterprise.gateway.config import GatewayRuntimeSettings, config
from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.dialect import exec_sql, fetchone

RUNTIME_SETTINGS_ROW_ID = 1
POLL_MIN_SECONDS = 0.5
POLL_MAX_SECONDS = 3600.0
TTL_MIN_SECONDS = 60
TTL_MAX_SECONDS = 30 * 24 * 60 * 60
QUALITY_TIMEOUT_MIN_SECONDS = 60
QUALITY_TIMEOUT_MAX_SECONDS = 7 * 24 * 60 * 60
DOCUMENT_MAX_MIB = 128
ATTACHMENT_MAX_MIB = 10


class RuntimeSettingsError(ValueError):
    """The stored or submitted runtime settings are invalid."""


def _section(payload: Mapping[str, Any], name: str, keys: set[str]) -> Mapping[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RuntimeSettingsError(f"invalid runtime settings section: {name}")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeSettingsError(f"invalid runtime setting: {name}")
    return value


def _float(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise RuntimeSettingsError(f"invalid runtime setting: {name}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise RuntimeSettingsError(f"invalid runtime setting: {name}") from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise RuntimeSettingsError(f"runtime setting outside safe range: {name}")
    return parsed


def _int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeSettingsError(f"invalid runtime setting: {name}")
    parsed = value
    if not minimum <= parsed <= maximum:
        raise RuntimeSettingsError(f"runtime setting outside safe range: {name}")
    return parsed


def parse_runtime_settings(payload: Mapping[str, Any]) -> GatewayRuntimeSettings:
    """Parse the public camelCase representation into the flat runtime model."""
    if not isinstance(payload, Mapping):
        raise RuntimeSettingsError("runtime settings must be an object")
    expected = {
        "outbox",
        "statusReconciler",
        "transientAttachmentCleanup",
        "qualityEvaluation",
        "qualityReconciler",
        "callbackDelivery",
        "limits",
        "diagnostics",
    }
    if set(payload) != expected:
        raise RuntimeSettingsError("runtime settings contain unknown or missing fields")

    outbox = _section(payload, "outbox", {"enabled", "pollSeconds"})
    status = _section(payload, "statusReconciler", {"enabled", "pollSeconds"})
    cleanup = _section(
        payload,
        "transientAttachmentCleanup",
        {"enabled", "pollSeconds", "ttlSeconds"},
    )
    quality = _section(payload, "qualityEvaluation", {"enabled", "pollSeconds"})
    reconciler = _section(
        payload,
        "qualityReconciler",
        {"enabled", "pollSeconds", "runningTimeoutSeconds"},
    )
    callback = _section(payload, "callbackDelivery", {"enabled", "pollSeconds"})
    limits = _section(
        payload,
        "limits",
        {"fileShareMaxMiB", "s3MaxMiB", "transientAttachmentMaxMiB"},
    )
    diagnostics = _section(payload, "diagnostics", {"enabled"})
    return GatewayRuntimeSettings(
        outbox_enabled=_bool(outbox["enabled"], "outbox.enabled"),
        outbox_poll_seconds=_float(
            outbox["pollSeconds"],
            "outbox.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        status_reconciler_enabled=_bool(
            status["enabled"], "statusReconciler.enabled"
        ),
        reconcile_seconds=_float(
            status["pollSeconds"],
            "statusReconciler.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        transient_cleanup_enabled=_bool(
            cleanup["enabled"], "transientAttachmentCleanup.enabled"
        ),
        attachment_cleanup_interval_seconds=_float(
            cleanup["pollSeconds"],
            "transientAttachmentCleanup.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        attachment_ttl_seconds=_int(
            cleanup["ttlSeconds"],
            "transientAttachmentCleanup.ttlSeconds",
            TTL_MIN_SECONDS,
            TTL_MAX_SECONDS,
        ),
        quality_worker_enabled=_bool(quality["enabled"], "qualityEvaluation.enabled"),
        quality_poll_seconds=_float(
            quality["pollSeconds"],
            "qualityEvaluation.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        quality_reconciler_enabled=_bool(
            reconciler["enabled"], "qualityReconciler.enabled"
        ),
        quality_reconcile_seconds=_float(
            reconciler["pollSeconds"],
            "qualityReconciler.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        quality_running_timeout_seconds=_int(
            reconciler["runningTimeoutSeconds"],
            "qualityReconciler.runningTimeoutSeconds",
            QUALITY_TIMEOUT_MIN_SECONDS,
            QUALITY_TIMEOUT_MAX_SECONDS,
        ),
        callback_enabled=_bool(callback["enabled"], "callbackDelivery.enabled"),
        callback_poll_seconds=_float(
            callback["pollSeconds"],
            "callbackDelivery.pollSeconds",
            POLL_MIN_SECONDS,
            POLL_MAX_SECONDS,
        ),
        file_share_max_size_mb=_int(
            limits["fileShareMaxMiB"],
            "limits.fileShareMaxMiB",
            1,
            DOCUMENT_MAX_MIB,
        ),
        s3_max_size_mb=_int(
            limits["s3MaxMiB"],
            "limits.s3MaxMiB",
            1,
            DOCUMENT_MAX_MIB,
        ),
        transient_attachment_max_size_mb=_int(
            limits["transientAttachmentMaxMiB"],
            "limits.transientAttachmentMaxMiB",
            1,
            ATTACHMENT_MAX_MIB,
        ),
        rag_diagnostics_enabled=_bool(diagnostics["enabled"], "diagnostics.enabled"),
    )


def normalize_runtime_settings(settings: GatewayRuntimeSettings) -> GatewayRuntimeSettings:
    """Clamp environment-derived defaults before they become persistent state."""
    return replace(
        settings,
        outbox_poll_seconds=max(
            POLL_MIN_SECONDS, min(float(settings.outbox_poll_seconds), POLL_MAX_SECONDS)
        ),
        reconcile_seconds=max(
            POLL_MIN_SECONDS, min(float(settings.reconcile_seconds), POLL_MAX_SECONDS)
        ),
        attachment_cleanup_interval_seconds=max(
            POLL_MIN_SECONDS,
            min(float(settings.attachment_cleanup_interval_seconds), POLL_MAX_SECONDS),
        ),
        attachment_ttl_seconds=max(
            TTL_MIN_SECONDS,
            min(int(settings.attachment_ttl_seconds), TTL_MAX_SECONDS),
        ),
        quality_poll_seconds=max(
            POLL_MIN_SECONDS, min(float(settings.quality_poll_seconds), POLL_MAX_SECONDS)
        ),
        quality_reconcile_seconds=max(
            POLL_MIN_SECONDS,
            min(float(settings.quality_reconcile_seconds), POLL_MAX_SECONDS),
        ),
        quality_running_timeout_seconds=max(
            QUALITY_TIMEOUT_MIN_SECONDS,
            min(int(settings.quality_running_timeout_seconds), QUALITY_TIMEOUT_MAX_SECONDS),
        ),
        callback_poll_seconds=max(
            POLL_MIN_SECONDS,
            min(float(settings.callback_poll_seconds), POLL_MAX_SECONDS),
        ),
        file_share_max_size_mb=max(1, min(int(settings.file_share_max_size_mb), DOCUMENT_MAX_MIB)),
        s3_max_size_mb=max(1, min(int(settings.s3_max_size_mb), DOCUMENT_MAX_MIB)),
        transient_attachment_max_size_mb=max(
            1, min(int(settings.transient_attachment_max_size_mb), ATTACHMENT_MAX_MIB)
        ),
    )


class RuntimeSettingsManager:
    """Load one settings row and publish an immutable snapshot to the process."""

    def __init__(self, gateway: GatewayDatabase) -> None:
        self.gateway = gateway
        self._settings: GatewayRuntimeSettings | None = None
        self._source = "environment"
        self._updated_at: str | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            async with self.gateway.transaction(write=True) as conn:
                row = await fetchone(
                    conn,
                    "SELECT settings_json, updated_at FROM gateway_runtime_settings "
                    "WHERE id=?",
                    (RUNTIME_SETTINGS_ROW_ID,),
                )
                if row is None:
                    settings = normalize_runtime_settings(
                        GatewayRuntimeSettings.from_config(config)
                    )
                    now = datetime.now(timezone.utc).isoformat()
                    await exec_sql(
                        conn,
                        "INSERT INTO gateway_runtime_settings "
                        "(id, settings_json, updated_at, updated_by) VALUES (?, ?, ?, NULL)",
                        (
                            RUNTIME_SETTINGS_ROW_ID,
                            json.dumps(settings.to_api(), ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
                    source = "environment"
                    updated_at = now
                else:
                    try:
                        stored = json.loads(str(row["settings_json"]))
                        if isinstance(stored, dict) and "diagnostics" not in stored:
                            # Backfill the field added after the first runtime-settings
                            # release without discarding any administrator choices.
                            stored["diagnostics"] = {
                                "enabled": os.getenv(
                                    "ENTERPRISE_RAG_DIAGNOSTICS_ENABLED", "false"
                                ).lower()
                                in ("1", "true", "yes", "on"),
                            }
                            await exec_sql(
                                conn,
                                "UPDATE gateway_runtime_settings SET settings_json=? "
                                "WHERE id=?",
                                (
                                    json.dumps(stored, ensure_ascii=False, sort_keys=True),
                                    RUNTIME_SETTINGS_ROW_ID,
                                ),
                            )
                        settings = parse_runtime_settings(stored)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeSettingsError(
                            "stored Gateway runtime settings are invalid"
                        ) from exc
                    source = "database"
                    updated_at = str(row.get("updated_at") or "") or None
            self._publish(settings, source=source, updated_at=updated_at)

    async def update(
        self,
        settings: GatewayRuntimeSettings,
        *,
        updated_by: str | None = None,
    ) -> None:
        settings = parse_runtime_settings(settings.to_api())
        await self.ensure_loaded()
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            async with self.gateway.transaction(write=True) as conn:
                await exec_sql(
                    conn,
                    "INSERT INTO gateway_runtime_settings "
                    "(id, settings_json, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (
                        RUNTIME_SETTINGS_ROW_ID,
                        json.dumps(settings.to_api(), ensure_ascii=False, sort_keys=True),
                        now,
                        (updated_by or "")[:128] or None,
                    ),
                )
            self._publish(settings, source="database", updated_at=now)

    def snapshot(self) -> GatewayRuntimeSettings:
        return self._settings or config.runtime_settings()

    def response(self) -> dict[str, Any]:
        return {
            "settings": self.snapshot().to_api(),
            "source": self._source,
            "updatedAt": self._updated_at,
            "hotReload": True,
        }

    def _publish(
        self,
        settings: GatewayRuntimeSettings,
        *,
        source: str,
        updated_at: str | None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._updated_at = updated_at
        self._loaded = True
        config.apply_runtime_settings(settings)


__all__ = [
    "ATTACHMENT_MAX_MIB",
    "DOCUMENT_MAX_MIB",
    "POLL_MAX_SECONDS",
    "POLL_MIN_SECONDS",
    "QUALITY_TIMEOUT_MAX_SECONDS",
    "QUALITY_TIMEOUT_MIN_SECONDS",
    "RuntimeSettingsError",
    "RuntimeSettingsManager",
    "TTL_MAX_SECONDS",
    "TTL_MIN_SECONDS",
    "normalize_runtime_settings",
    "parse_runtime_settings",
]
