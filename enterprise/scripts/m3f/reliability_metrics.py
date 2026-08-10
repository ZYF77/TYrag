"""Calculate M3-F performance evidence without contacting external services.

The input is deliberately a small measurement envelope containing timings and
statuses only. The tool never accepts or emits document text, prompts, model
responses, credentials, or request bodies. Missing real measurements are
reported as blocked rather than converted into a passing baseline.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


class ReliabilityInputError(ValueError):
    """The measurement or target contract is invalid."""


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReliabilityInputError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ReliabilityInputError(f"{field} must be finite and >= {minimum}")
    return result


def percentile(values: list[float], quantile: float) -> float:
    """Return a deterministic nearest-rank percentile."""
    if not values:
        raise ReliabilityInputError("percentile requires at least one value")
    if not 0 < quantile <= 1:
        raise ReliabilityInputError("quantile must be in the interval (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def summarize_samples(
    samples: list[dict[str, Any]],
    *,
    window_seconds: float,
    concurrency: int,
) -> dict[str, Any]:
    """Summarize latency, queue, throughput, and error evidence."""
    window = _number(window_seconds, "window_seconds", minimum=0.000001)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ReliabilityInputError("concurrency must be a positive integer")
    if not isinstance(samples, list) or not samples:
        raise ReliabilityInputError("samples must contain at least one item")

    latencies: list[float] = []
    queue_latencies: list[float] = []
    errors = 0
    cost_units = 0.0
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ReliabilityInputError(f"sample {index} must be an object")
        latencies.append(_number(sample.get("latency_ms"), f"samples[{index}].latency_ms"))
        queue_latencies.append(
            _number(
                sample.get("queue_latency_ms", 0),
                f"samples[{index}].queue_latency_ms",
            )
        )
        success = sample.get("success")
        if not isinstance(success, bool):
            raise ReliabilityInputError(f"samples[{index}].success must be boolean")
        if not success:
            errors += 1
        if "cost_units" in sample:
            cost_units += _number(sample["cost_units"], f"samples[{index}].cost_units")

    count = len(samples)
    successful = count - errors
    return {
        "sample_count": count,
        "success_count": successful,
        "error_count": errors,
        "concurrency": concurrency,
        "window_seconds": window,
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "queue_latency_ms": {
            "p50": percentile(queue_latencies, 0.50),
            "p95": percentile(queue_latencies, 0.95),
            "p99": percentile(queue_latencies, 0.99),
        },
        "throughput_rps": successful / window,
        "attempted_throughput_rps": count / window,
        "error_rate": errors / count,
        "cost_units": cost_units,
    }


def validate_baseline_config(config: dict[str, Any]) -> None:
    """Validate the committed target contract, not a measured result."""
    if config.get("schema_version") != 1:
        raise ReliabilityInputError("performance baseline schema_version must be 1")
    if config.get("status") != "initial_target_not_measured":
        raise ReliabilityInputError("performance baseline must remain explicitly unmeasured")
    rules = config.get("measurement_rules")
    if not isinstance(rules, dict) or rules.get("percentile") != "nearest_rank":
        raise ReliabilityInputError("nearest-rank percentile rule is required")
    workloads = config.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        raise ReliabilityInputError("at least one performance workload is required")

    seen: set[str] = set()
    for workload in workloads:
        if not isinstance(workload, dict):
            raise ReliabilityInputError("each workload must be an object")
        workload_id = workload.get("id")
        if not isinstance(workload_id, str) or not workload_id or workload_id in seen:
            raise ReliabilityInputError("workload ids must be non-empty and unique")
        seen.add(workload_id)
        concurrency = workload.get("concurrency")
        if (
            not isinstance(concurrency, list)
            or not concurrency
            or any(not isinstance(value, int) or value < 1 for value in concurrency)
        ):
            raise ReliabilityInputError(f"{workload_id} concurrency matrix is invalid")
        _number(workload.get("duration_seconds"), f"{workload_id}.duration_seconds", minimum=1)
        targets = workload.get("targets")
        if not isinstance(targets, dict):
            raise ReliabilityInputError(f"{workload_id}.targets is required")
        for metric in ("latency_ms", "queue_latency_ms"):
            limits = targets.get(metric)
            if not isinstance(limits, dict):
                raise ReliabilityInputError(f"{workload_id}.{metric} targets are required")
            for percentile_name in ("p50_max", "p95_max", "p99_max"):
                _number(limits.get(percentile_name), f"{workload_id}.{metric}.{percentile_name}")
        _number(targets.get("throughput_rps_min"), f"{workload_id}.throughput_rps_min")
        error_limit = _number(targets.get("error_rate_max"), f"{workload_id}.error_rate_max")
        if error_limit > 1:
            raise ReliabilityInputError(f"{workload_id}.error_rate_max must be <= 1")
        evidence = workload.get("required_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ReliabilityInputError(f"{workload_id}.required_evidence is required")


def evaluate_summary(summary: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one measured summary against one target block."""
    checks: dict[str, bool] = {
        "latency_p50": summary["latency_ms"]["p50"] <= targets["latency_ms"]["p50_max"],
        "latency_p95": summary["latency_ms"]["p95"] <= targets["latency_ms"]["p95_max"],
        "latency_p99": summary["latency_ms"]["p99"] <= targets["latency_ms"]["p99_max"],
        "queue_p50": summary["queue_latency_ms"]["p50"] <= targets["queue_latency_ms"]["p50_max"],
        "queue_p95": summary["queue_latency_ms"]["p95"] <= targets["queue_latency_ms"]["p95_max"],
        "queue_p99": summary["queue_latency_ms"]["p99"] <= targets["queue_latency_ms"]["p99_max"],
        "throughput": summary["throughput_rps"] >= targets["throughput_rps_min"],
        "error_rate": summary["error_rate"] <= targets["error_rate_max"],
    }
    return {"status": "accepted" if all(checks.values()) else "failed", "checks": checks}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReliabilityInputError("JSON input cannot be read") from exc
    if not isinstance(payload, dict):
        raise ReliabilityInputError("JSON root must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    baseline = _load_json(baseline_path)
    validate_baseline_config(baseline)
    if not args.input:
        payload = {
            "status": "blocked",
            "reason": "real measurement input is required; no synthetic result was generated",
            "baseline": str(baseline_path),
        }
        if args.output:
            _write_json(Path(args.output), payload)
        print(json.dumps(payload, separators=(",", ":")))
        return 2

    measurement = _load_json(Path(args.input))
    if measurement.get("schema_version") != 1:
        raise ReliabilityInputError("measurement schema_version must be 1")
    workload_id = measurement.get("workload_id")
    workload = next(
        (item for item in baseline["workloads"] if item["id"] == workload_id),
        None,
    )
    if workload is None:
        raise ReliabilityInputError("measurement workload_id is not in the baseline")
    summary = summarize_samples(
        measurement.get("samples"),
        window_seconds=measurement.get("window_seconds"),
        concurrency=measurement.get("concurrency"),
    )
    evaluation = evaluate_summary(summary, workload["targets"])
    payload = {
        "schema_version": 1,
        "status": evaluation["status"],
        "workload_id": workload_id,
        "environment_version": measurement.get("environment_version", "unreported"),
        "gateway_instance_count": measurement.get("gateway_instance_count", "unreported"),
        "redis_valkey_mode": measurement.get("redis_valkey_mode", "unreported"),
        "object_storage_mode": measurement.get("object_storage_mode", "unreported"),
        "summary": summary,
        "evaluation": evaluation,
    }
    if args.output:
        _write_json(Path(args.output), payload)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if evaluation["status"] == "accepted" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_baseline = Path(__file__).with_name("performance_baseline.json")
    parser.add_argument("--baseline", default=str(default_baseline))
    parser.add_argument("--input", help="real, sanitized timing envelope")
    parser.add_argument("--output", help="sanitized summary artifact")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ReliabilityInputError:
        print(
            json.dumps(
                {"status": "failed", "reason": "reliability input contract failed"},
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
