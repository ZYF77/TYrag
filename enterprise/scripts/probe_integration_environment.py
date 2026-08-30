"""Fail-closed preflight for the required FILE_SHARE/v2 Integration profile.

The probe reports only component names, state labels, and stable reasons.  It
never prints configuration values, response bodies, exception text, or secret
material.  Exit codes are intentionally small and stable for the PowerShell
runner: 0 means every component is available, 3 means the environment is
missing or unavailable, and 4 means the probe itself failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


CONFIGURED = "configured"
MISSING = "missing"
UNAVAILABLE = "unavailable"


def _state(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _first_value(*names: str) -> str:
    for name in names:
        value = _value(name)
        if value:
            return value
    return ""


def _url_is_valid(value: str, schemes: set[str]) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in schemes and bool(parsed.hostname)


def _file_share_config() -> tuple[dict[str, Path] | None, dict[str, str]]:
    raw = _value("ENTERPRISE_FILE_SHARE_ROOTS")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, _state(UNAVAILABLE, "root_registry_invalid")
        if not isinstance(parsed, dict) or not parsed:
            return None, _state(UNAVAILABLE, "root_registry_invalid")
        roots: dict[str, Path] = {}
        for root_id, root_path in parsed.items():
            if not isinstance(root_id, str) or not root_id.strip():
                return None, _state(UNAVAILABLE, "root_registry_invalid")
            if not isinstance(root_path, str) or not root_path.strip():
                return None, _state(UNAVAILABLE, "root_registry_invalid")
            roots[root_id.strip()] = Path(root_path)
    else:
        root = _value("ENTERPRISE_FILE_SHARE_ROOT")
        root_id = _value("ENTERPRISE_FILE_SHARE_ROOT_ID") or "default"
        if not root:
            return None, _state(MISSING, "root_registry")
        roots = {root_id: Path(root)}

    if any(not path.is_dir() or not os.access(path, os.R_OK) for path in roots.values()):
        return roots, _state(UNAVAILABLE, "root_unreadable")
    return roots, _state(CONFIGURED, "root_registry")


async def _database_state() -> dict[str, str]:
    try:
        from sqlalchemy import text

        from enterprise.gateway.db.database import GatewayDatabase

        # GatewayDatabase resolves ENTERPRISE_GATEWAY_DATABASE_URL or the
        # ENTERPRISE_GATEWAY_DB_HOST/PORT/NAME/USER/PASSWORD component settings.
        gateway = GatewayDatabase.from_env()
        conn = await gateway.connect()
        try:
            await conn.execute(text("SELECT 1"))
        finally:
            await conn.close()
            await gateway.dispose()
    except Exception:
        return _state(UNAVAILABLE, "gateway_postgresql_unavailable")
    return _state(CONFIGURED, "gateway_postgresql")


def _auth_state() -> dict[str, str]:
    if _value("ENTERPRISE_TEST_MODE") == "1":
        return _state(UNAVAILABLE, "test_mode_not_allowed")
    if _value("ENTERPRISE_SYNC_AUTH_ENABLED").lower() == "false":
        return _state(UNAVAILABLE, "service_auth_disabled")

    credentials = _value("ENTERPRISE_SYNC_HMAC_CREDENTIALS")
    if not credentials:
        return _state(MISSING, "hmac_credentials")
    try:
        parsed = json.loads(credentials)
        if isinstance(parsed, dict):
            parsed = parsed.get("credentials") if "credentials" in parsed else [parsed]
        if not isinstance(parsed, list) or not parsed:
            return _state(UNAVAILABLE, "hmac_credentials_invalid")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _state(UNAVAILABLE, "hmac_credentials_invalid")

    issuer = _value("JWT_ISSUER")
    audience = _value("JWT_AUDIENCE")
    if not issuer or not audience:
        return _state(MISSING, "jwt_issuer_or_audience")
    jwks_url = _value("JWT_JWKS_URL")
    hs_enabled = _value("JWT_ENABLE_HS").lower() == "true"
    shared_secret = _value("JWT_SHARED_SECRET")
    if not jwks_url and (not hs_enabled or not shared_secret):
        return _state(MISSING, "jwt_verification_key")
    if jwks_url and not _url_is_valid(jwks_url, {"http", "https"}):
        return _state(UNAVAILABLE, "jwt_jwks_url")
    return _state(CONFIGURED, "hmac_and_jwt")


async def _probe_http(
    *,
    base_url: str,
    path: str,
    accepted_statuses: set[int],
    headers: dict[str, str] | None = None,
) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(f"{base_url.rstrip('/')}{path}", headers=headers)
    except httpx.HTTPError:
        return False
    return response.status_code in accepted_statuses


async def _probe_services() -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}

    ragflow_base = _first_value("ENTERPRISE_RAGFLOW_BASE_URL", "RAGFLOW_BASE_URL")
    ragflow_key = _first_value("ENTERPRISE_RAGFLOW_API_KEY", "RAGFLOW_API_KEY")
    if not ragflow_base or not ragflow_key:
        results["ragflow"] = _state(MISSING, "base_url_or_api_key")
    elif not _url_is_valid(ragflow_base, {"http", "https"}):
        results["ragflow"] = _state(UNAVAILABLE, "base_url_invalid")
    elif await _probe_http(
        base_url=ragflow_base,
        path="/api/v1/system/version",
        accepted_statuses=set(range(200, 300)),
        headers={"Authorization": f"Bearer {ragflow_key}"},
    ):
        results["ragflow"] = _state(CONFIGURED, "api_reachable")
    else:
        results["ragflow"] = _state(UNAVAILABLE, "api_unreachable")

    asset_base = _value("ENTERPRISE_EAM_ASSET_RESOLVER_BASE_URL")
    if asset_base:
        if not _url_is_valid(asset_base, {"http", "https"}):
            results["eamAssetResolver"] = _state(UNAVAILABLE, "base_url_invalid")
        else:
            asset_headers = {"Accept": "application/json"}
            asset_token = _value("ENTERPRISE_EAM_ASSET_RESOLVER_TOKEN")
            if asset_token:
                asset_headers["Authorization"] = f"Bearer {asset_token}"
            resolver_path = (
                _value("ENTERPRISE_EAM_ASSET_RESOLVER_PATH")
                or "/api/integration/v1/assets/resolve"
            )
            reachable = await _probe_http(
                base_url=asset_base,
                path=f"{resolver_path}?equipmentId=tyrag-integration-probe",
                accepted_statuses={200, 404},
                headers=asset_headers,
            )
            results["eamAssetResolver"] = _state(
                CONFIGURED if reachable else UNAVAILABLE,
                "resolver_reachable" if reachable else "resolver_unreachable",
            )

    gateway = _value("GATEWAY_URL")
    if not gateway:
        results["gateway"] = _state(MISSING, "base_url")
    elif not _url_is_valid(gateway, {"http", "https"}):
        results["gateway"] = _state(UNAVAILABLE, "base_url_invalid")
    else:
        reachable = await _probe_http(
            base_url=gateway,
            path="/enterprise/api/v1/health",
            accepted_statuses={200},
        )
        results["gateway"] = _state(
            CONFIGURED if reachable else UNAVAILABLE,
            "health_reachable" if reachable else "health_unreachable",
        )
    return results


async def _probe_redis() -> dict[str, str]:
    url = _value("ENTERPRISE_REDIS_URL")
    if not url:
        return _state(MISSING, "redis_url")
    if not _url_is_valid(url, {"redis", "rediss"}):
        return _state(UNAVAILABLE, "redis_url_invalid")

    from enterprise.gateway.auth.service_auth import (
        RedisReplayStore,
        ReplayStoreUnavailable,
    )

    prefix = f"tyrag:integration-preflight:{uuid.uuid4().hex}:"
    first = RedisReplayStore(url, prefix=prefix, timeout=3.0)
    second = RedisReplayStore(url, prefix=prefix, timeout=3.0)
    key = uuid.uuid4().hex
    try:
        first_result = await first.reserve(key, 0)
        second_result = await second.reserve(key, 0)
    except (ReplayStoreUnavailable, ValueError):
        return _state(UNAVAILABLE, "shared_replay_store")
    if first_result is True and second_result is False:
        return _state(CONFIGURED, "atomic_set_nx")
    return _state(UNAVAILABLE, "atomic_set_nx_failed")


async def _run() -> tuple[int, dict[str, object]]:
    file_roots, file_share = _file_share_config()
    del file_roots
    database = await _database_state()
    auth = _auth_state()

    evidence: dict[str, dict[str, str]] = {
        "fileShare": file_share,
        "database": database,
        "auth": auth,
    }
    evidence.update(await _probe_services())
    evidence["redis"] = await _probe_redis()

    missing = sorted(name for name, item in evidence.items() if item["status"] == MISSING)
    unavailable = sorted(
        name for name, item in evidence.items() if item["status"] == UNAVAILABLE
    )
    passed = not missing and not unavailable
    payload = {
        "profile": "Integration",
        "passed": passed,
        "evidence": evidence,
        "missing": missing,
        "unavailable": unavailable,
    }
    return (0 if passed else 3), payload


def main() -> int:
    try:
        code, payload = asyncio.run(_run())
    except Exception:
        code, payload = 4, {
            "profile": "Integration",
            "passed": False,
            "evidence": {"probe": _state(UNAVAILABLE, "tool_error")},
            "missing": [],
            "unavailable": ["probe"],
        }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
