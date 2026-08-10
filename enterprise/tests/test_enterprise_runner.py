"""Offline contract tests for the enterprise acceptance runner itself."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "enterprise" / "scripts" / "run_enterprise_tests.ps1"


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    raise AssertionError("PowerShell is required to test the acceptance runner")


def _run_runner(tmp_path: Path, profile: str, env: dict[str, str] | None = None):
    artifact_root = tmp_path / "artifacts"
    command = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(RUNNER),
        "-Profile",
        profile,
        "-PythonPath",
        sys.executable,
        "-ArtifactRoot",
        str(artifact_root),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    summaries = sorted(artifact_root.glob("*/summary.json"))
    summary = json.loads(summaries[-1].read_text(encoding="utf-8")) if summaries else None
    return result, summary


def _integration_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ENTERPRISE_RAGFLOW_BASE_URL": "http://127.0.0.1:9380",
            "ENTERPRISE_RAGFLOW_API_KEY": "runner-test-ragflow-key",
            "ENTERPRISE_ASSET_REGISTRY_BASE_URL": "http://127.0.0.1:9390",
            "ENTERPRISE_REDIS_URL": "redis://127.0.0.1:6379/0",
            "GATEWAY_URL": "http://127.0.0.1:5188",
            "S3_ENDPOINT": "http://127.0.0.1:9000",
            "S3_ACCESS_KEY": "runner-test-access-key",
            "S3_SECRET_KEY": "runner-test-secret-key",
            "S3_BUCKET": "runner-test-bucket",
            "ENTERPRISE_SYNC_SERVICE_TOKEN": "runner-test-service-token",
            "JWT_SHARED_SECRET": "runner-test-jwt-secret",
        }
    )
    return env


def test_profile_parameter_rejects_unknown_profile(tmp_path):
    result, summary = _run_runner(tmp_path, "NotAProfile")
    assert result.returncode == 4
    assert summary is None


def test_missing_integration_environment_returns_exit_three(tmp_path):
    env = os.environ.copy()
    for name in (
        "ENTERPRISE_RAGFLOW_BASE_URL",
        "ENTERPRISE_RAGFLOW_API_KEY",
        "ENTERPRISE_ASSET_REGISTRY_BASE_URL",
        "ENTERPRISE_REDIS_URL",
        "GATEWAY_URL",
        "S3_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "ENTERPRISE_SYNC_SERVICE_TOKEN",
        "JWT_SHARED_SECRET",
    ):
        env.pop(name, None)
    result, summary = _run_runner(tmp_path, "Integration", env)
    assert result.returncode == 3
    assert summary["profile"] == "Integration"
    assert summary["passed"] is False
    assert summary["gitCommit"]
    assert isinstance(summary["worktreeDirty"], bool)


def test_invalid_integration_url_returns_exit_three(tmp_path):
    env = _integration_env()
    env["ENTERPRISE_RAGFLOW_BASE_URL"] = "not-a-url"
    result, summary = _run_runner(tmp_path, "Integration", env)
    assert result.returncode == 3
    assert summary["passed"] is False
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "artifacts").rglob("*")
        if path.is_file()
    )
    assert "runner-test-ragflow-key" not in artifact_text
    assert "runner-test-secret-key" not in artifact_text


def test_contract_artifact_binds_current_head_and_dirty_state(tmp_path):
    result, summary = _run_runner(tmp_path, "Contract")
    assert result.returncode == 0
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    worktree_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert summary["gitCommit"] == expected_head
    assert summary["worktreeDirty"] is bool(worktree_status)
    assert summary["profile"] == "Contract"
    assert summary["passed"] is True
    assert summary["evidenceSummary"]
    assert summary["evidence"]["ragflowGuardUnchanged"] is True
    assert "worktreeChangeCountBefore" in summary["evidence"]
    assert "trackedChangeCountBefore" not in summary["evidence"]


def test_runner_rejects_skips_and_xpasses_in_test_steps():
    source = RUNNER.read_text(encoding="utf-8")
    assert "--untracked-files=all" in source
    assert "--untracked-files=no" not in source
    assert "xfail_strict=true" in source
    assert "counts.skipped -gt 0" in source
    assert "--runxfail" not in source
    assert "pytest-live-integration" in source
    assert "Assert-NoIntegrationBypassTests" in source
    assert "probe_integration_environment.py" in source
    assert "run_wp04_phase2_e2e.py" in source
