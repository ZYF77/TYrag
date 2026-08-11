"""Offline contract tests for the enterprise acceptance runner itself."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "enterprise" / "scripts" / "run_enterprise_tests.ps1"
PREFLIGHT = ROOT / "enterprise" / "scripts" / "probe_integration_environment.py"
LIVE_SUITE = ROOT / "enterprise" / "scripts" / "run_file_share_v3_v2_e2e.py"
OVERLAY = ROOT / "deploy" / "overlays" / "docker-compose.enterprise.yml"


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    raise AssertionError("PowerShell is required to test the acceptance runner")


def _run_runner(
    tmp_path: Path,
    profile: str,
    env: dict[str, str] | None = None,
    artifact_root: Path | str | None = None,
):
    artifact_root_arg = artifact_root or (tmp_path / "artifacts")
    artifact_root_path = Path(artifact_root_arg)
    if not artifact_root_path.is_absolute():
        artifact_root_path = ROOT / artifact_root_path
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
        str(artifact_root_arg),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    summaries = sorted(artifact_root_path.glob("*/summary.json"))
    summary = json.loads(summaries[-1].read_text(encoding="utf-8")) if summaries else None
    return result, summary


def _integration_env(tmp_path: Path) -> dict[str, str]:
    file_share_root = tmp_path / "file-share"
    file_share_root.mkdir()
    state_host_dir = tmp_path / "gateway-state"
    state_host_dir.mkdir()
    db_path = state_host_dir / "gateway.db"
    env = os.environ.copy()
    env.update(
        {
            "ENTERPRISE_RAGFLOW_BASE_URL": "http://127.0.0.1:9380",
            "ENTERPRISE_RAGFLOW_API_KEY": "runner-test-ragflow-key",
            "ENTERPRISE_ASSET_REGISTRY_BASE_URL": "http://127.0.0.1:9390",
            "ENTERPRISE_REDIS_URL": "redis://127.0.0.1:6379/0",
            "GATEWAY_URL": "http://127.0.0.1:5188",
            "ENTERPRISE_FILE_SHARE_ROOTS": json.dumps(
                {"device-share": str(file_share_root)}
            ),
            "ENTERPRISE_GATEWAY_STATE_HOST_DIR": str(state_host_dir),
            "ENTERPRISE_DB_PATH": str(db_path),
            "ENTERPRISE_SYNC_DB_PATH": str(db_path),
            "ENTERPRISE_SYNC_HMAC_CREDENTIALS": "runner-test-hmac-config",
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
        "ENTERPRISE_FILE_SHARE_ROOTS",
        "ENTERPRISE_FILE_SHARE_ROOT",
        "ENTERPRISE_FILE_SHARE_ROOT_ID",
        "ENTERPRISE_GATEWAY_STATE_HOST_DIR",
        "ENTERPRISE_DB_PATH",
        "ENTERPRISE_SYNC_DB_PATH",
        "ENTERPRISE_SYNC_HMAC_CREDENTIALS",
        "JWT_SHARED_SECRET",
    ):
        env.pop(name, None)
    result, summary = _run_runner(tmp_path, "Integration", env)
    assert result.returncode == 3
    assert summary["profile"] == "Integration"
    assert summary["passed"] is False
    assert summary["offlineImplementationTestsExist"] is True
    assert summary["offlineImplementationTestsRequested"] is True
    assert summary["offlineImplementationTestsExecuted"] is False
    assert summary["p1Status"] == "offline_implementation_tests_not_reached"
    assert summary["requiredIntegrationEvidence"] is False
    assert summary["requiredIntegrationEvidenceReason"]
    assert summary["gitCommit"]
    assert isinstance(summary["worktreeDirty"], bool)


def test_invalid_integration_url_returns_exit_three(tmp_path):
    env = _integration_env(tmp_path)
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
    assert "runner-test-hmac-config" not in artifact_text


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
    assert summary["offlineImplementationTestsExist"] is True
    assert summary["offlineImplementationTestsRequested"] is False
    assert summary["offlineImplementationTestsExecuted"] is False
    assert summary["p1Status"] == "not_requested"
    assert summary["m3LiveIntegrationEvidence"] is False
    assert summary["evidenceSummary"]
    assert summary["evidence"]["ragflowGuardUnchanged"] is True
    assert "worktreeChangeCountBefore" in summary["evidence"]
    assert "trackedChangeCountBefore" not in summary["evidence"]


def test_relative_artifact_root_is_normalized_before_child_tools_run(tmp_path):
    artifact_root = tmp_path / "relative-artifacts"
    relative_root = os.path.relpath(artifact_root, ROOT)

    result, summary = _run_runner(
        tmp_path,
        "Contract",
        artifact_root=relative_root,
    )

    assert result.returncode == 0
    assert summary["passed"] is True
    assert Path(summary["artifacts"]["summary"]).is_absolute()
    assert list(artifact_root.glob("*/junit/contract-static.xml"))


def test_runner_rejects_skips_and_xpasses_in_test_steps():
    source = RUNNER.read_text(encoding="utf-8")
    assert "--untracked-files=all" in source
    assert "--untracked-files=no" not in source
    assert "xfail_strict=true" in source
    assert "counts.skipped -gt 0" in source
    assert "--runxfail" not in source
    assert "live-file-share-v3-v2" in source
    assert "Assert-NoIntegrationBypassTests" in source
    assert "probe_integration_environment.py" in source
    assert "run_file_share_v3_v2_e2e.py" in source
    assert "S3_ENDPOINT" not in source
    assert "offlineImplementationTests" in source
    assert "FILE_SHARE v3 + formal v2" in source
    assert "no P1 implementation tests executed" not in source


def test_required_live_suite_is_v3_v2_and_has_strict_status_url_path():
    source = LIVE_SUITE.read_text(encoding="utf-8")
    assert "/enterprise/api/v3/documents" in source
    assert "/enterprise/api/v2/conversations" in source
    assert "X-TY-Signature" in source
    assert "statusUrl" in source
    assert "statusUrl" in source
    assert "status_url" in source
    assert 'status.get("retrievable") is True' in source
    assert 'status.get("status") == "retrievable"' not in source
    assert "S3_" not in source
    assert "/enterprise/api/v1/" not in source
    assert "pytest.skip" not in source
    assert "pytest.xfail" not in source
    assert "unittest.mock" not in source
    assert "Mock(" not in source


def test_preflight_reports_only_states_and_has_no_s3_requirements():
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "ENTERPRISE_FILE_SHARE_ROOTS" in source
    assert "ENTERPRISE_ASSET_REGISTRY_BASE_URL" in source
    assert "ENTERPRISE_RAGFLOW_BASE_URL" in source
    assert "ENTERPRISE_REDIS_URL" in source
    assert "GATEWAY_URL" in source
    assert "ENTERPRISE_DB_PATH" in source
    assert "ENTERPRISE_SYNC_DB_PATH" in source
    assert "ENTERPRISE_GATEWAY_STATE_HOST_DIR" in source
    assert "database_path_not_shared" in source
    assert "S3_ENDPOINT" not in source
    assert "S3_ACCESS_KEY" not in source
    assert "S3_SECRET_KEY" not in source
    assert "configured" in source
    assert "missing" in source
    assert "unavailable" in source


def test_overlay_wires_gateway_file_share_and_internal_ticket_network():
    overlay_source = OVERLAY.read_text(encoding="utf-8")
    overlay = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    services = overlay["services"]
    gateway = services["enterprise-gateway"]
    ragflow = services["ragflow-cpu"]
    assert "ragflow" in gateway["networks"]
    assert any(":ro" in str(volume) for volume in gateway["volumes"])
    assert gateway["environment"]["RAGFLOW_BASE_URL"] == "http://ragflow-cpu:9380"
    assert gateway["environment"]["TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY"]
    assert gateway["environment"]["ENTERPRISE_SYNC_AUTH_ENABLED"] == "true"
    assert gateway["environment"].get("ENTERPRISE_TEST_MODE") != "1"
    assert ragflow["environment"]["TYRAG_EXTERNAL_SOURCE_GATEWAY_URL"] == (
        "http://enterprise-gateway:5188"
    )
    assert ragflow["environment"]["TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY"]
    assert (
        "${ENTERPRISE_GATEWAY_STATE_HOST_DIR:?set ENTERPRISE_GATEWAY_STATE_HOST_DIR}:/var/lib/tyrag/state"
        in overlay_source
    )
    assert "enterprise_gateway_state" not in overlay_source


def test_unshared_gateway_db_returns_exit_three(tmp_path):
    env = _integration_env(tmp_path)
    other_state_dir = tmp_path / "other-state"
    other_state_dir.mkdir()
    env["ENTERPRISE_DB_PATH"] = str(other_state_dir / "gateway.db")
    result, summary = _run_runner(tmp_path, "Integration", env)
    assert result.returncode == 3
    assert summary["requiredIntegrationEvidence"] is False


def test_status_url_fixture_is_server_owned_and_scope_checked():
    from enterprise.scripts.run_file_share_v3_v2_e2e import (
        LiveAssertionError,
        validate_status_url,
    )

    scope = {
        "tenant_id": "tyrag-integration",
        "source_system": "EAM",
        "external_document_id": "DOC-1",
        "source_version_id": "v3-1",
    }
    server_url = (
        "/enterprise/api/v3/documents/DOC-1/status"
        "?tenantId=tyrag-integration&sourceSystem=EAM&sourceVersionId=v3-1"
    )
    assert validate_status_url({"statusUrl": server_url}, **scope) == server_url

    with pytest.raises(LiveAssertionError):
        validate_status_url({}, **scope)
    with pytest.raises(LiveAssertionError):
        validate_status_url(
            {"statusUrl": server_url.replace("DOC-1", "OTHER")}, **scope
        )
