"""Offline contract evaluator for the proposed M3-F rate-limit policy.

This is intentionally not imported by the Gateway runtime. It makes the
policy and its negative cases executable without claiming that production
requests are already throttled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RateLimitPolicyError(ValueError):
    """The rate-limit policy or an offline request envelope is invalid."""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    status_code: int
    code: str
    dimension: str | None
    retry_after_seconds: int


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = path or Path(__file__).with_name("rate_limit_policy.json")
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RateLimitPolicyError("rate-limit policy cannot be read") from exc
    if not isinstance(payload, dict):
        raise RateLimitPolicyError("rate-limit policy root must be an object")
    validate_policy(payload)
    return payload


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != 1:
        raise RateLimitPolicyError("rate-limit policy schema_version must be 1")
    if policy.get("status") != "proposed_not_enforced":
        raise RateLimitPolicyError("policy must not claim runtime enforcement")
    dimensions = policy.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != {"tenant", "user", "api_key", "cost"}:
        raise RateLimitPolicyError("tenant, user, api_key, and cost dimensions are required")
    for name in ("tenant", "user", "api_key"):
        limits = dimensions[name]
        if (
            not isinstance(limits, dict)
            or not isinstance(limits.get("window_seconds"), int)
            or limits["window_seconds"] < 1
            or not isinstance(limits.get("max_requests"), int)
            or limits["max_requests"] < 1
            or not isinstance(limits.get("max_concurrent"), int)
            or limits["max_concurrent"] < 1
        ):
            raise RateLimitPolicyError(f"{name} request and concurrency limits are invalid")
    cost = dimensions["cost"]
    if (
        not isinstance(cost, dict)
        or not isinstance(cost.get("window_seconds"), int)
        or cost["window_seconds"] < 1
        or not isinstance(cost.get("max_cost_units"), int)
        or cost["max_cost_units"] < 1
    ):
        raise RateLimitPolicyError("cost limit is invalid")
    failure_policy = policy.get("failure_policy")
    if not isinstance(failure_policy, dict):
        raise RateLimitPolicyError("failure_policy is required")
    if failure_policy.get("redis_valkey_unavailable") != "fail_closed":
        raise RateLimitPolicyError("protected routes must fail closed without Redis/Valkey")
    if failure_policy.get("never_fallback_to_process_memory") is not True:
        raise RateLimitPolicyError("process-memory fallback must remain disabled")
    response = policy.get("response")
    if not isinstance(response, dict) or response.get("http_status") != 429:
        raise RateLimitPolicyError("rate-limit response must be HTTP 429")


def _subject(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RateLimitPolicyError(f"{name} is required for a protected request")
    return value.strip()


def evaluate_request(
    policy: dict[str, Any],
    request: dict[str, Any],
    usage: dict[str, dict[str, int]],
    *,
    redis_valkey_available: bool = True,
) -> RateLimitDecision:
    """Evaluate a request against already reserved counters.

    ``usage`` is evidence input, not a local counter implementation. A real
    adapter must reserve all dimensions atomically in Redis/Valkey and roll
    back a partial reservation before invoking the protected operation.
    """
    validate_policy(policy)
    route_class = request.get("route_class")
    if not isinstance(route_class, str) or not route_class:
        raise RateLimitPolicyError("route_class is required")
    if route_class == "health":
        return RateLimitDecision(True, 200, "HEALTH_NOT_LIMITED", None, 0)
    if route_class not in policy.get("failure_policy", {}).get("protected_routes", []):
        raise RateLimitPolicyError("route_class is not declared in the policy")
    if not redis_valkey_available:
        return RateLimitDecision(False, 503, "RATE_LIMIT_STORE_UNAVAILABLE", "store", 1)

    for field in ("tenant_id", "user_id", "api_key_id"):
        _subject(request, field)
    cost_units = request.get("cost_units")
    if isinstance(cost_units, bool) or not isinstance(cost_units, int) or cost_units < 0:
        raise RateLimitPolicyError("cost_units must be a non-negative integer")

    dimensions = policy["dimensions"]
    checks = (
        ("api_key", "api_key_id"),
        ("user", "user_id"),
        ("tenant", "tenant_id"),
    )
    for dimension, _subject_field in checks:
        limits = dimensions[dimension]
        current = usage.get(dimension, {})
        requests = current.get("requests", 0)
        concurrent = current.get("concurrent", 0)
        if not isinstance(requests, int) or requests < 0 or not isinstance(concurrent, int) or concurrent < 0:
            raise RateLimitPolicyError(f"{dimension} usage is invalid")
        if requests + 1 > limits["max_requests"]:
            return RateLimitDecision(False, 429, "RATE_LIMITED", dimension, limits["window_seconds"])
        if concurrent + 1 > limits["max_concurrent"]:
            return RateLimitDecision(False, 429, "RATE_LIMITED", dimension, limits["window_seconds"])

    cost_usage = usage.get("cost", {}).get("cost_units", 0)
    if not isinstance(cost_usage, int) or cost_usage < 0:
        raise RateLimitPolicyError("cost usage is invalid")
    if cost_usage + cost_units > dimensions["cost"]["max_cost_units"]:
        return RateLimitDecision(
            False,
            429,
            "RATE_LIMITED",
            "cost",
            dimensions["cost"]["window_seconds"],
        )
    return RateLimitDecision(True, 200, "ALLOWED", None, 0)
