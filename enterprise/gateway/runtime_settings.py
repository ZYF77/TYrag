"""Persisted, hot-reloadable Gateway runtime settings."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from enterprise.gateway.config import (
    RETRIEVAL_SCOPE_POLICIES,
    GatewayRuntimeSettings,
    config,
    ragflow_status_webhook_enabled_from_env,
    ragflow_status_webhook_ignore_cancel_from_env,
    ragflow_status_webhook_secret_from_env,
    retrieval_scope_policy_from_env,
    user_memory_enabled_from_env,
    user_memory_id_from_env,
    user_memory_timeout_seconds_from_env,
    user_memory_top_n_from_env,
    workflow_agent_id_from_env,
    workflow_enabled_from_env,
    workflow_timeout_seconds_from_env,
    workflow_version_from_env,
)
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
USER_MEMORY_TOP_N_MIN = 1
USER_MEMORY_TOP_N_MAX = 20
USER_MEMORY_TIMEOUT_MIN_SECONDS = 0.5
USER_MEMORY_TIMEOUT_MAX_SECONDS = 120.0
WORKFLOW_TIMEOUT_MIN_SECONDS = 1.0
WORKFLOW_TIMEOUT_MAX_SECONDS = 600.0


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


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeSettingsError(f"invalid runtime setting: {name}")
    return value



def _parse_ragflow_status_webhook(
    payload: Mapping[str, Any],
    *,
    previous_secret: str | None = None,
) -> tuple[bool, str, bool]:
    """Parse inbound RF status webhook section.

    Public GET shape uses ``secretConfigured`` (never plaintext). PUT may send
    write-only ``secret``; empty/omitted keeps ``previous_secret``.
    """
    value = payload.get("ragflowStatusWebhook")
    if not isinstance(value, Mapping):
        raise RuntimeSettingsError("invalid runtime settings section: ragflowStatusWebhook")
    allowed = {"enabled", "ignoreCancel", "secret", "secretConfigured"}
    if not set(value) <= allowed or "enabled" not in value or "ignoreCancel" not in value:
        raise RuntimeSettingsError("invalid runtime settings section: ragflowStatusWebhook")
    enabled = _bool(value["enabled"], "ragflowStatusWebhook.enabled")
    ignore_cancel = _bool(value["ignoreCancel"], "ragflowStatusWebhook.ignoreCancel")
    secret = previous_secret or ""
    if "secret" in value and value["secret"] is not None:
        if not isinstance(value["secret"], str):
            raise RuntimeSettingsError("invalid runtime setting: ragflowStatusWebhook.secret")
        # Non-empty write updates; empty string keeps the previous secret.
        if value["secret"]:
            secret = value["secret"]
    return enabled, secret, ignore_cancel


def parse_runtime_settings(
    payload: Mapping[str, Any],
    *,
    previous_secret: str | None = None,
) -> GatewayRuntimeSettings:
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
        "retrievalScope",
        "ragflowStatusWebhook",
        "userMemory",
        "workflow",
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
    retrieval_scope = _section(payload, "retrievalScope", {"policy"})
    policy_raw = retrieval_scope["policy"]
    if not isinstance(policy_raw, str):
        raise RuntimeSettingsError("invalid runtime setting: retrievalScope.policy")
    policy = policy_raw.strip().lower()
    if policy not in RETRIEVAL_SCOPE_POLICIES:
        # Match env/config: illegal values fall back to the safe default.
        policy = "legacy_device"
    webhook_enabled, webhook_secret, webhook_ignore_cancel = _parse_ragflow_status_webhook(
        payload, previous_secret=previous_secret
    )
    user_memory = _section(
        payload,
        "userMemory",
        {"enabled", "memoryId", "topN", "timeoutSeconds"},
    )
    workflow = _section(
        payload,
        "workflow",
        {"enabled", "agentId", "version", "timeoutSeconds"},
    )
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
        retrieval_scope_policy=policy,
        ragflow_status_webhook_enabled=webhook_enabled,
        ragflow_status_webhook_secret=webhook_secret,
        ragflow_status_webhook_ignore_cancel=webhook_ignore_cancel,
        user_memory_enabled=_bool(user_memory["enabled"], "userMemory.enabled"),
        user_memory_id=_str(user_memory["memoryId"], "userMemory.memoryId").strip(),
        user_memory_top_n=_int(
            user_memory["topN"],
            "userMemory.topN",
            USER_MEMORY_TOP_N_MIN,
            USER_MEMORY_TOP_N_MAX,
        ),
        user_memory_timeout_seconds=_float(
            user_memory["timeoutSeconds"],
            "userMemory.timeoutSeconds",
            USER_MEMORY_TIMEOUT_MIN_SECONDS,
            USER_MEMORY_TIMEOUT_MAX_SECONDS,
        ),
        workflow_enabled=_bool(workflow["enabled"], "workflow.enabled"),
        workflow_agent_id=_str(workflow["agentId"], "workflow.agentId").strip(),
        workflow_version=_str(workflow["version"], "workflow.version").strip(),
        workflow_timeout_seconds=_float(
            workflow["timeoutSeconds"],
            "workflow.timeoutSeconds",
            WORKFLOW_TIMEOUT_MIN_SECONDS,
            WORKFLOW_TIMEOUT_MAX_SECONDS,
        ),
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
        user_memory_top_n=max(
            USER_MEMORY_TOP_N_MIN,
            min(int(settings.user_memory_top_n), USER_MEMORY_TOP_N_MAX),
        ),
        user_memory_timeout_seconds=max(
            USER_MEMORY_TIMEOUT_MIN_SECONDS,
            min(float(settings.user_memory_timeout_seconds), USER_MEMORY_TIMEOUT_MAX_SECONDS),
        ),
        user_memory_id=str(settings.user_memory_id or "").strip(),
        user_memory_enabled=bool(settings.user_memory_enabled),
        workflow_enabled=bool(settings.workflow_enabled),
        workflow_agent_id=str(settings.workflow_agent_id or "").strip(),
        workflow_version=str(settings.workflow_version or "").strip(),
        workflow_timeout_seconds=max(
            WORKFLOW_TIMEOUT_MIN_SECONDS,
            min(float(settings.workflow_timeout_seconds), WORKFLOW_TIMEOUT_MAX_SECONDS),
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
                            json.dumps(settings.to_storage(), ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
                    source = "environment"
                    updated_at = now
                else:
                    try:
                        stored = json.loads(str(row["settings_json"]))
                        if isinstance(stored, dict):
                            mutated = False
                            if "diagnostics" not in stored:
                                # Backfill the field added after the first runtime-settings
                                # release without discarding any administrator choices.
                                stored["diagnostics"] = {
                                    "enabled": os.getenv(
                                        "ENTERPRISE_RAG_DIAGNOSTICS_ENABLED", "false"
                                    ).lower()
                                    in ("1", "true", "yes", "on"),
                                }
                                mutated = True
                            if "retrievalScope" not in stored:
                                stored["retrievalScope"] = {
                                    "policy": retrieval_scope_policy_from_env(),
                                }
                                mutated = True
                            if "ragflowStatusWebhook" not in stored:
                                stored["ragflowStatusWebhook"] = {
                                    "enabled": ragflow_status_webhook_enabled_from_env(),
                                    "secret": ragflow_status_webhook_secret_from_env(),
                                    "ignoreCancel": ragflow_status_webhook_ignore_cancel_from_env(),
                                }
                                mutated = True
                            if "userMemory" not in stored:
                                stored["userMemory"] = {
                                    "enabled": user_memory_enabled_from_env(),
                                    "memoryId": user_memory_id_from_env(),
                                    "topN": user_memory_top_n_from_env(),
                                    "timeoutSeconds": user_memory_timeout_seconds_from_env(),
                                }
                                mutated = True
                            if "workflow" not in stored:
                                stored["workflow"] = {
                                    "enabled": workflow_enabled_from_env(),
                                    "agentId": workflow_agent_id_from_env(),
                                    "version": workflow_version_from_env(),
                                    "timeoutSeconds": workflow_timeout_seconds_from_env(),
                                }
                                mutated = True
                            if mutated:
                                await exec_sql(
                                    conn,
                                    "UPDATE gateway_runtime_settings SET settings_json=? "
                                    "WHERE id=?",
                                    (
                                        json.dumps(
                                            stored, ensure_ascii=False, sort_keys=True
                                        ),
                                        RUNTIME_SETTINGS_ROW_ID,
                                    ),
                                )
                        # Storage may include plaintext secret; public shape uses
                        # secretConfigured only — fall back to env secret when absent.
                        stored_webhook = stored.get("ragflowStatusWebhook") if isinstance(stored, dict) else None
                        prior_secret = None
                        if isinstance(stored_webhook, dict) and isinstance(stored_webhook.get("secret"), str):
                            prior_secret = stored_webhook.get("secret")
                        settings = parse_runtime_settings(
                            stored,
                            previous_secret=prior_secret
                            if prior_secret is not None
                            else ragflow_status_webhook_secret_from_env(),
                        )
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
        # Validate via storage shape so the write-only secret survives round-trip.
        settings = parse_runtime_settings(settings.to_storage())
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
                        json.dumps(settings.to_storage(), ensure_ascii=False, sort_keys=True),
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
    "USER_MEMORY_TIMEOUT_MAX_SECONDS",
    "USER_MEMORY_TIMEOUT_MIN_SECONDS",
    "USER_MEMORY_TOP_N_MAX",
    "USER_MEMORY_TOP_N_MIN",
    "WORKFLOW_TIMEOUT_MAX_SECONDS",
    "WORKFLOW_TIMEOUT_MIN_SECONDS",
    "RuntimeSettingsError",
    "RuntimeSettingsManager",
    "TTL_MAX_SECONDS",
    "TTL_MIN_SECONDS",
    "normalize_runtime_settings",
    "parse_runtime_settings",
]
