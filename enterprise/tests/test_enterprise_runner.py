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
    env = os.environ.copy()
    env.update(
        {
            "ENTERPRISE_RAGFLOW_BASE_URL": "http://127.0.0.1:9380",
            "ENTERPRISE_RAGFLOW_API_KEY": "runner-test-ragflow-key",
            "ENTERPRISE_REDIS_URL": "redis://127.0.0.1:6379/0",
            "GATEWAY_URL": "http://127.0.0.1:5188",
            "ENTERPRISE_FILE_SHARE_ROOTS": json.dumps(
                {"device-share": str(file_share_root)}
            ),
            "ENTERPRISE_GATEWAY_STATE_HOST_DIR": str(state_host_dir),
            "ENTERPRISE_GATEWAY_DATABASE_URL": (
                "postgresql+asyncpg://runner_test:runner_test@127.0.0.1:55432/runner_test"
            ),
            "ENTERPRISE_GATEWAY_DATABASE_SCHEMA": "runner_test",
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
        "ENTERPRISE_REDIS_URL",
        "GATEWAY_URL",
        "ENTERPRISE_FILE_SHARE_ROOTS",
        "ENTERPRISE_FILE_SHARE_ROOT",
        "ENTERPRISE_FILE_SHARE_ROOT_ID",
        "ENTERPRISE_GATEWAY_STATE_HOST_DIR",
        "ENTERPRISE_GATEWAY_DATABASE_URL",
        "ENTERPRISE_GATEWAY_DATABASE_SCHEMA",
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
    assert "applicationEnvironmentNames" in source
    assert "ENTERPRISE_TEST_MODE', '1', 'Process'" in source
    assert 'Remove-Item -LiteralPath "Env:$environmentName"' in source
    assert "Remove-Item -LiteralPath 'Env:ENTERPRISE_TEST_MODE'" in source
    assert "RAGFLOW_" in source
    assert "TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY" not in source


def test_required_live_suite_is_v3_v2_and_has_accept_receipt_plus_diagnostic_status():
    source = LIVE_SUITE.read_text(encoding="utf-8")
    assert "/enterprise/api/v3/documents" in source
    assert "/enterprise/api/v2/conversations" in source
    assert "X-TY-Signature" in source
    assert "validate_accept_receipt" in source
    assert "build_diagnostic_status_url" in source
    assert "status_url" in source
    assert 'status.get("retrievable") is True' in source
    assert 'status.get("pipelineStatus", "")' in source
    assert 'status.get("parseCompleted") is not True' in source
    assert 'status.get("indexCompleted") is not True' in source
    assert 'status.get("qualityStatus") != "passed"' in source
    assert 'status.get("errorCode") is not None' in source
    assert 'status.get("status") == "retrievable"' not in source
    assert "S3_" not in source
    assert "/enterprise/api/v1/health" in source
    assert "/api/v1/system/version" in source
    assert 'actual != "v0.26.4"' in source
    assert "parse_sse" in source
    assert "answer.completed" in source
    assert "_assert_history_replay" in source
    assert "_verify_ragflow_session" in source
    assert "_verify_document_scoped_retrieval" in source
    assert '"document_ids": [document_id]' in source
    assert "pytest.skip" not in source
    assert "pytest.xfail" not in source
    assert "unittest.mock" not in source
    assert "Mock(" not in source
    assert '"reason": str(exc)' in source


def test_live_suite_accepts_asset_scope_citations_but_requires_new_document(tmp_path):
    from enterprise.scripts.run_file_share_v3_v2_e2e import (
        _stage_unique_source_copy,
        matching_ingested_citations,
    )

    citations = [
        {
            "citationId": "old",
            "externalDocumentId": "DOC-OLD",
            "sourceVersionId": "v1",
        },
        {
            "citationId": "new",
            "externalDocumentId": "DOC-NEW",
            "sourceVersionId": "v2",
        },
    ]

    assert matching_ingested_citations(
        citations,
        external_document_id="DOC-NEW",
        source_version_id="v2",
    ) == [citations[1]]
    assert matching_ingested_citations(
        citations,
        external_document_id="DOC-MISSING",
        source_version_id="v2",
    ) == []

    source = tmp_path / "manual.pdf"
    source.write_bytes(b"test-pdf-bytes")
    staged, relative = _stage_unique_source_copy(source, "manual.pdf")
    try:
        assert staged.read_bytes() == source.read_bytes()
        assert staged.name != source.name
        assert relative == staged.name
    finally:
        staged.unlink(missing_ok=True)


def test_preflight_reports_only_states_and_has_no_s3_requirements():
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "ENTERPRISE_FILE_SHARE_ROOTS" in source
    assert "ENTERPRISE_EAM_ASSET_RESOLVER_BASE_URL" in source
    assert "ENTERPRISE_RAGFLOW_BASE_URL" in source
    assert "ENTERPRISE_REDIS_URL" in source
    assert "GATEWAY_URL" in source
    assert "ENTERPRISE_GATEWAY_DATABASE_URL" in source
    assert "ENTERPRISE_GATEWAY_DB_HOST" in source
    assert "gateway_postgresql_unavailable" in source
    assert "S3_ENDPOINT" not in source
    assert "S3_ACCESS_KEY" not in source
    assert "S3_SECRET_KEY" not in source
    assert "configured" in source
    assert "missing" in source
    assert "unavailable" in source


@pytest.mark.parametrize(
    "credentials",
    (
        {"credentialId": "runner-test"},
        {"credentials": [{"credentialId": "runner-test"}]},
    ),
)
def test_preflight_accepts_supported_hmac_credential_shapes(
    monkeypatch, credentials
):
    from enterprise.scripts.probe_integration_environment import _auth_state

    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
    monkeypatch.setenv("ENTERPRISE_SYNC_AUTH_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_SYNC_HMAC_CREDENTIALS", json.dumps(credentials))
    monkeypatch.setenv("JWT_ISSUER", "runner-test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "runner-test-audience")
    monkeypatch.setenv("JWT_ENABLE_HS", "true")
    monkeypatch.setenv("JWT_SHARED_SECRET", "runner-test-secret")

    assert _auth_state() == {"status": "configured", "reason": "hmac_and_jwt"}


def test_overlay_wires_gateway_file_share_and_official_ragflow_api():
    overlay_source = OVERLAY.read_text(encoding="utf-8")
    overlay = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    services = overlay["services"]
    gateway = services["enterprise-gateway"]
    assert "ragflow" in gateway["networks"]
    assert any(":ro" in str(volume) for volume in gateway["volumes"])
    assert gateway["environment"]["RAGFLOW_BASE_URL"] == "http://ragflow-cpu:9380"
    assert gateway["environment"]["ENTERPRISE_SYNC_AUTH_ENABLED"] == "true"
    assert gateway["environment"]["ENTERPRISE_CALLBACK_ENABLED"]
    assert gateway["environment"]["ENTERPRISE_CALLBACK_ENDPOINTS"]
    assert gateway["environment"].get("ENTERPRISE_TEST_MODE") != "1"
    assert "TYRAG_EXTERNAL_SOURCE" not in overlay_source
    assert (
        "${ENTERPRISE_GATEWAY_STATE_HOST_DIR:?set ENTERPRISE_GATEWAY_STATE_HOST_DIR}:/var/lib/tyrag/state"
        in overlay_source
    )
    assert "enterprise_gateway_state" not in overlay_source


def test_unshared_gateway_db_returns_exit_three(tmp_path):
    env = _integration_env(tmp_path)
    env["ENTERPRISE_GATEWAY_DATABASE_URL"] = (
        "postgresql+asyncpg://runner_test:runner_test@127.0.0.1:55433/runner_test"
    )
    result, summary = _run_runner(tmp_path, "Integration", env)
    assert result.returncode == 3
    assert summary["requiredIntegrationEvidence"] is False


def test_accept_receipt_and_diagnostic_status_url_helpers():
    from enterprise.scripts.run_file_share_v3_v2_e2e import (
        LiveAssertionError,
        build_diagnostic_status_url,
        validate_accept_receipt,
    )

    scope = {
        "tenant_id": "tyrag-integration",
        "source_system": "EAM",
        "external_document_id": "DOC-1",
        "source_version_id": "v3-1",
    }
    assert build_diagnostic_status_url(**scope) == (
        "/enterprise/api/v3/documents/DOC-1/status"
        "?tenantId=tyrag-integration&sourceSystem=EAM&sourceVersionId=v3-1"
    )
    validate_accept_receipt(
        {
            "operationId": "evt-1",
            "externalDocumentId": "DOC-1",
            "sourceVersionId": "v3-1",
            "deduplicated": False,
            "updatedAt": "2026-01-01T00:00:00+00:00",
        },
        external_document_id="DOC-1",
        source_version_id="v3-1",
    )
    with pytest.raises(LiveAssertionError):
        validate_accept_receipt(
            {
                "operationId": "evt-1",
                "externalDocumentId": "DOC-1",
                "sourceVersionId": "v3-1",
                "deduplicated": False,
                "updatedAt": "2026-01-01T00:00:00+00:00",
                "statusUrl": "/enterprise/api/v3/documents/DOC-1/status",
            },
            external_document_id="DOC-1",
            source_version_id="v3-1",
        )


def test_live_runner_docker_target_allowlist():
    from enterprise.scripts.run_file_share_v3_v2_e2e import (
        LiveEnvironmentError,
        assert_http_target,
    )

    assert assert_http_target("http://enterprise-gateway:5188", target_mode="docker")
    assert assert_http_target("http://ragflow-cpu:9380", target_mode="docker")
    with pytest.raises(LiveEnvironmentError):
        assert_http_target("http://192.168.30.30:5188", target_mode="docker")
    with pytest.raises(LiveEnvironmentError):
        assert_http_target("http://example.com", target_mode="docker")


def test_existing_callback_mode_reads_only_safe_delivery_fields(monkeypatch, tmp_path):
    from enterprise.scripts.run_file_share_v3_v2_e2e import (
        Artifacts,
        ExistingCallbackDelivery,
    )

    monkeypatch.setattr(
        "enterprise.scripts.run_file_share_v3_v2_e2e._db_row",
        lambda _query, _scope: {
            "delivery_id": "delivery-1",
            "attempts": 2,
            "state": "delivered",
            "last_http_status": 204,
        },
    )
    artifacts = Artifacts(tmp_path / "artifacts")
    ExistingCallbackDelivery(
        "tenant-a", "EAM", "DOC-1", "v1", artifacts,
    ).assert_success(1)
    assert artifacts.callbacks == [
        {"deliveryId": "delivery-1", "attempts": 2, "httpStatus": 204}
    ]


def test_live_runner_uses_actual_pdf_page_count():
    from enterprise.scripts.run_file_share_v3_v2_e2e import _pdf_page_count

    assert _pdf_page_count(ROOT / "enterprise" / "tests" / "fixtures" / "Doc1.pdf") == 1
