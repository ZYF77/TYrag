"""Offline M3-F reliability asset and policy contract tests.

These tests validate deterministic helpers and committed contracts only. They
do not report Integration acceptance and do not replace live dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enterprise.scripts.m3f.backup_restore_drill import (
    BackupValidationError,
    calculate_rpo_seconds,
    create_backup,
    restore_backup,
    verify_manifest,
)
from enterprise.scripts.m3f.rate_limit_policy import (
    evaluate_request,
    load_policy,
)
from enterprise.scripts.m3f.reliability_metrics import (
    evaluate_summary,
    summarize_samples,
    validate_baseline_config,
)


ROOT = Path(__file__).resolve().parents[2]
M3F = ROOT / "enterprise" / "scripts" / "m3f"


def test_performance_baseline_is_explicitly_unmeasured_and_complete():
    config = json.loads((M3F / "performance_baseline.json").read_text(encoding="utf-8"))
    validate_baseline_config(config)
    assert config["status"] == "initial_target_not_measured"
    assert {"p50", "p95", "p99"} <= set(config["required_report_fields"])
    assert {"queue_latency_p50", "queue_latency_p95", "queue_latency_p99"} <= set(
        config["required_report_fields"]
    )
    assert all(item["concurrency"] for item in config["workloads"])


def test_percentiles_throughput_queue_latency_and_error_rate_are_deterministic():
    samples = [
        {
            "latency_ms": number,
            "queue_latency_ms": number / 2,
            "success": number != 100,
            "cost_units": 1,
        }
        for number in range(1, 101)
    ]
    summary = summarize_samples(samples, window_seconds=10, concurrency=25)
    assert summary["latency_ms"] == {"p50": 50.0, "p95": 95.0, "p99": 99.0}
    assert summary["queue_latency_ms"] == {"p50": 25.0, "p95": 47.5, "p99": 49.5}
    assert summary["success_count"] == 99
    assert summary["error_rate"] == pytest.approx(0.01)
    assert summary["throughput_rps"] == pytest.approx(9.9)


def test_performance_target_failure_is_visible():
    summary = summarize_samples(
        [
            {"latency_ms": 2500, "queue_latency_ms": 1500, "success": True}
            for _ in range(10)
        ],
        window_seconds=1,
        concurrency=10,
    )
    result = evaluate_summary(
        summary,
        {
            "latency_ms": {"p50_max": 1000, "p95_max": 1000, "p99_max": 1000},
            "queue_latency_ms": {"p50_max": 1000, "p95_max": 1000, "p99_max": 1000},
            "throughput_rps_min": 20,
            "error_rate_max": 0.01,
        },
    )
    assert result["status"] == "failed"
    assert result["checks"]["latency_p95"] is False
    assert result["checks"]["throughput"] is False


def _request(route_class: str = "query", *, cost_units: int = 1) -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "api_key_id": "key-a",
        "route_class": route_class,
        "cost_units": cost_units,
    }


def test_rate_limit_policy_has_all_dimensions_and_fail_closed_rule():
    policy = load_policy()
    assert set(policy["dimensions"]) == {"tenant", "user", "api_key", "cost"}
    assert policy["failure_policy"]["redis_valkey_unavailable"] == "fail_closed"
    assert policy["failure_policy"]["never_fallback_to_process_memory"] is True


@pytest.mark.parametrize(
    ("dimension", "usage"),
    [
        ("tenant", {"tenant": {"requests": 600, "concurrent": 0}}),
        ("user", {"user": {"requests": 120, "concurrent": 0}}),
        ("api_key", {"api_key": {"requests": 300, "concurrent": 0}}),
        ("cost", {"cost": {"cost_units": 100000}}),
    ],
)
def test_rate_limit_negative_cases_reject_each_dimension(dimension, usage):
    decision = evaluate_request(load_policy(), _request(cost_units=1), usage)
    assert decision.allowed is False
    assert decision.status_code == 429
    assert decision.code == "RATE_LIMITED"
    assert decision.dimension == dimension
    assert decision.retry_after_seconds > 0


def test_rate_limit_concurrency_and_redis_failure_are_negative_cases():
    policy = load_policy()
    decision = evaluate_request(
        policy,
        _request(),
        {"user": {"requests": 0, "concurrent": 8}},
    )
    assert decision.allowed is False
    assert decision.dimension == "user"

    unavailable = evaluate_request(
        policy,
        _request(),
        {},
        redis_valkey_available=False,
    )
    assert unavailable.allowed is False
    assert unavailable.status_code == 503
    assert unavailable.code == "RATE_LIMIT_STORE_UNAVAILABLE"


def test_rate_limit_health_route_is_not_a_protected_request():
    decision = evaluate_request(
        load_policy(),
        {"route_class": "health"},
        {},
        redis_valkey_available=False,
    )
    assert decision.allowed is True
    assert decision.status_code == 200


def test_backup_manifest_detects_tampering_and_restores(tmp_path):
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    restore = tmp_path / "restore"
    (source / "objects").mkdir(parents=True)
    (source / "state.json").write_text('{"schema":1}\n', encoding="utf-8")
    (source / "objects" / "object-1.bin").write_bytes(b"sanitized-object")

    created = create_backup(
        source,
        backup,
        source_watermark="2026-08-10T10:00:00Z",
    )
    assert created["status"] == "passed"
    assert verify_manifest(backup)["status"] == "passed"

    (backup / "state.json").write_text("tampered\n", encoding="utf-8")
    tampered = verify_manifest(backup)
    assert tampered["status"] == "failed"
    assert tampered["mismatch_count"] == 1

    (backup / "state.json").write_text('{"schema":1}\n', encoding="utf-8")
    restored = restore_backup(backup, restore)
    assert restored["status"] == "passed"
    assert (restore / "objects" / "object-1.bin").read_bytes() == b"sanitized-object"


def test_backup_restore_refuses_non_empty_target_and_reports_rpo(tmp_path):
    backup = tmp_path / "backup"
    restore = tmp_path / "restore"
    backup.mkdir()
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "captured_at": "2026-08-10T10:00:00Z",
                "source_watermark": "2026-08-10T10:00:00Z",
                "file_count": 0,
                "total_bytes": 0,
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    restore.mkdir()
    (restore / "preexisting").write_text("do-not-delete", encoding="utf-8")
    with pytest.raises(BackupValidationError):
        restore_backup(backup, restore)

    with pytest.raises(BackupValidationError):
        calculate_rpo_seconds("2026-08-10T09:59:00Z", "2026-08-10T10:00:00Z")

    assert calculate_rpo_seconds(
        "2026-08-10T10:15:00Z", "2026-08-10T10:00:00Z"
    ) == 900

    # A missing manifest is also a hard validation failure.
    with pytest.raises(BackupValidationError):
        restore_backup(tmp_path / "missing-m3f-backup", tmp_path / "missing-m3f-restore")


def test_runner_and_upgrade_entry_contracts_remain_strict():
    runner = (ROOT / "enterprise/scripts/run_enterprise_tests.ps1").read_text(
        encoding="utf-8"
    )
    upgrade = (ROOT / "enterprise/scripts/run_upgrade_checks.ps1").read_text(
        encoding="utf-8"
    )
    assert "$AllowedProfiles = @('Contract', 'P0', 'Integration', 'WP03', 'All')" in runner
    assert "'3' = 'missing or invalid external integration environment'" in runner
    assert "Set-ExitCode 3" in runner
    assert "counts.skipped -gt 0" in runner
    assert "-Profile 'Contract'" in upgrade
    assert "-Profile 'P0'" in upgrade
    assert "-BaselineSummary" in upgrade
    assert "'3' = 'external environment unavailable; preserve runner exit 3'" in upgrade
    assert "never_fallback_to_process_memory" in (
        M3F / "rate_limit_policy.json"
    ).read_text(encoding="utf-8")
