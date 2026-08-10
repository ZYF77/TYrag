"""Probe the real services required by the Integration profile.

Only status labels and HTTP status codes are printed. Credentials, response
bodies, URLs with embedded credentials, and exception text are intentionally
excluded from the evidence stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from urllib.parse import urlsplit

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _url_is_valid(value: str, schemes: set[str]) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in schemes and bool(parsed.hostname)


def _result(passed: bool, status: int | str, reason: str) -> dict[str, object]:
    return {"passed": passed, "status": status, "reason": reason}


async def _probe_http_services() -> dict[str, dict[str, object]]:
    ragflow_base = os.environ.get("ENTERPRISE_RAGFLOW_BASE_URL", "").strip().rstrip("/")
    ragflow_key = os.environ.get("ENTERPRISE_RAGFLOW_API_KEY", "").strip()
    asset_base = os.environ.get(
        "ENTERPRISE_ASSET_REGISTRY_BASE_URL", ""
    ).strip().rstrip("/")
    asset_token = os.environ.get("ENTERPRISE_ASSET_REGISTRY_TOKEN", "").strip()

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        try:
            ragflow_response = await client.get(
                f"{ragflow_base}/api/v1/system/version",
                headers={"Authorization": f"Bearer {ragflow_key}"},
            )
            ragflow = _result(
                200 <= ragflow_response.status_code < 300,
                ragflow_response.status_code,
                "public_version_endpoint",
            )
        except httpx.HTTPError:
            ragflow = _result(False, "unreachable", "public_version_endpoint")

        headers = {"Accept": "application/json"}
        if asset_token:
            headers["Authorization"] = f"Bearer {asset_token}"
        try:
            asset_response = await client.get(
                f"{asset_base}/v1/assets/resolve",
                params={
                    "tenantId": "tyrag-integration-probe",
                    "equipmentId": "tyrag-integration-probe",
                },
                headers=headers,
            )
            # A 404 is valid evidence for a live resolver when the synthetic
            # probe identifier does not exist. A successful response is also
            # accepted; no response body is persisted.
            asset = _result(
                asset_response.status_code in {200, 404},
                asset_response.status_code,
                "resolver_endpoint",
            )
        except httpx.HTTPError:
            asset = _result(False, "unreachable", "resolver_endpoint")
    return {"ragflow": ragflow, "assetRegistry": asset}


async def _probe_redis() -> dict[str, object]:
    from enterprise.gateway.auth.service_auth import (
        RedisReplayStore,
        ReplayStoreUnavailable,
    )

    url = os.environ.get("ENTERPRISE_REDIS_URL", "").strip()
    prefix = f"tyrag:integration-probe:{uuid.uuid4().hex}:"
    first = RedisReplayStore(url, prefix=prefix, timeout=3.0)
    second = RedisReplayStore(url, prefix=prefix, timeout=3.0)
    key = uuid.uuid4().hex
    try:
        first_result = await first.remember(key, 0)
        second_result = await second.remember(key, 0)
    except (ReplayStoreUnavailable, ValueError):
        return _result(False, "unavailable", "shared_replay_store")
    return _result(
        first_result is True and second_result is False,
        "atomic_set_nx",
        "shared_replay_store",
    )


async def _run() -> tuple[int, dict[str, object]]:
    required = (
        "ENTERPRISE_RAGFLOW_BASE_URL",
        "ENTERPRISE_RAGFLOW_API_KEY",
        "ENTERPRISE_ASSET_REGISTRY_BASE_URL",
        "ENTERPRISE_REDIS_URL",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        return 3, {
            "passed": False,
            "evidence": {"environment": _result(False, "missing", "required_variables")},
        }

    if not _url_is_valid(
        os.environ["ENTERPRISE_RAGFLOW_BASE_URL"].strip(), {"http", "https"}
    ) or not _url_is_valid(
        os.environ["ENTERPRISE_ASSET_REGISTRY_BASE_URL"].strip(), {"http", "https"}
    ) or not _url_is_valid(
        os.environ["ENTERPRISE_REDIS_URL"].strip(), {"redis", "rediss"}
    ):
        return 3, {
            "passed": False,
            "evidence": {"environment": _result(False, "invalid", "url_syntax")},
        }

    evidence = await _probe_http_services()
    evidence["redis"] = await _probe_redis()
    passed = all(bool(item["passed"]) for item in evidence.values())
    return (0 if passed else 3), {"passed": passed, "evidence": evidence}


def main() -> int:
    try:
        code, payload = asyncio.run(_run())
    except Exception:
        # Keep tool failures distinguishable without exposing exception text.
        code, payload = 4, {"passed": False, "evidence": {"probe": "tool_error"}}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
