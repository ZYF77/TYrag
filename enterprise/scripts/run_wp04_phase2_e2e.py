"""Committed WP-04 Phase 2 E2E runner.

Starts the Enterprise Gateway with in-process file logging, runs the formal
Phase 2 E2E script, and stops the Gateway afterwards. A clean checkout can
reproduce the run with only the documented environment variables plus a
deployed RAGFlow/MinIO:

    python enterprise/scripts/run_wp04_phase2_e2e.py

Local evidence (JSON report, doc_ids trace, gateway log) is written
under artifacts/ and is intentionally not committed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts"
WP03_SAMPLE_DIR = ARTIFACTS / "wp03" / "samples"
WP03_SAMPLE_IDS = ("wp03-digital_text-001", "wp03-digital_text-002")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "5196")
GATEWAY_URL = os.environ.get(
    "GATEWAY_URL", f"http://127.0.0.1:{GATEWAY_PORT}"
)
RAGFLOW_BASE_URL = os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380")
RAGFLOW_ADMIN_URL = os.environ.get(
    "RAGFLOW_ADMIN_URL", "http://127.0.0.1:9381"
)
RAGFLOW_ADMIN_EMAIL = os.environ.get(
    "RAGFLOW_ADMIN_EMAIL", "admin@ragflow.io"
)
RAGFLOW_ADMIN_PASSWORD = os.environ.get("RAGFLOW_ADMIN_PASSWORD", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
DB_SCHEMA = f"gw_wp04_phase2_{time.strftime('%Y%m%d_%H%M%S')}"
DATABASE_URL = os.environ.get(
    "ENTERPRISE_GATEWAY_DATABASE_URL",
    "postgresql+asyncpg://tyrag_gateway_test:tyrag_gateway_test@127.0.0.1:55432/tyrag_gateway_test",
)
REPORT_PATH = str(ARTIFACTS / "wp04-phase2-e2e.json")
QUERY_TRACE = ARTIFACTS / "wp04-phase2-docids-trace.log"
RUN_LOG = ARTIFACTS / "wp04-phase2-run.log"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "https://auth.example.com")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "tyrag-gateway")
SERVICE_TOKEN = os.environ.get(
    "ENTERPRISE_SYNC_SERVICE_TOKEN", ""
) or secrets.token_hex(24)
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "") or secrets.token_hex(32)
API_KEY = os.environ.get("RAGFLOW_API_KEY", "").strip()
SAMPLE_PREPARATION_MODE = ""
SAMPLE_SHA256_JSON = "{}"


def _sample_paths(sample_dir: Path | None = None) -> list[Path]:
    base = sample_dir or WP03_SAMPLE_DIR
    return [base / f"{sample_id}.pdf" for sample_id in WP03_SAMPLE_IDS]


def _samples_present(sample_dir: Path | None = None) -> bool:
    return all(
        path.exists() and path.stat().st_size > 0
        for path in _sample_paths(sample_dir)
    )


def _sample_sha256(sample_dir: Path | None = None) -> dict[str, str]:
    return {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _sample_paths(sample_dir)
    }


def _assert_clean_tracked_worktree() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("git status failed; cannot verify clean tracked worktree")
    if result.stdout.strip():
        raise RuntimeError(
            "WP-04 Phase2 formal E2E requires a clean tracked worktree.\n"
            + result.stdout.strip()
        )


def _run_wp03_sample_generator() -> int:
    result = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "enterprise"
                / "scripts"
                / "wp03"
                / "generate_samples.py"
            ),
            "--output-dir",
            str(WP03_SAMPLE_DIR),
            "--only",
            ",".join(WP03_SAMPLE_IDS),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        _log(
            "WP-03 sample generator failed:\n"
            f"{result.stdout.strip()}\n{result.stderr.strip()}"
        )
    return result.returncode


def prepare_wp03_samples(
    sample_dir: Path | None = None,
    generator: Callable[[], int] | None = None,
) -> dict:
    """Return the sample preparation mode and SHA-256 digest map."""
    sample_dir = sample_dir or WP03_SAMPLE_DIR
    if _samples_present(sample_dir):
        return {
            "mode": "existing",
            "sha256": _sample_sha256(sample_dir),
        }
    if generator is None:
        raise RuntimeError("WP-03 sample PDFs missing and no generator provided")
    _log("WP-04 E2E samples missing; generating WP-03 deterministic samples...")
    returncode = generator()
    if returncode != 0:
        raise RuntimeError(
            f"WP-03 sample generator failed with exit code {returncode}"
        )
    if not _samples_present(sample_dir):
        raise RuntimeError(
            "WP-03 sample generator exited 0 but did not create required PDFs: "
            + ", ".join(str(path) for path in _sample_paths(sample_dir))
        )
    _log("WP-04 E2E samples ready.")
    return {
        "mode": "generated",
        "sha256": _sample_sha256(sample_dir),
    }


def _log(line: str) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _resolve_api_key() -> str:
    if API_KEY:
        return API_KEY
    sys.path.insert(0, str(ROOT))
    from enterprise.scripts.wp04_phase2_e2e import (
        admin_token,
        fetch_api_key,
    )

    return fetch_api_key(admin_token(), RAGFLOW_ADMIN_EMAIL)


def _gateway_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_PORT": GATEWAY_PORT,
            "ENTERPRISE_DEMO_ROUTES_ENABLED": "true",
            "ENTERPRISE_GATEWAY_DATABASE_URL": DATABASE_URL,
            "ENTERPRISE_GATEWAY_DATABASE_SCHEMA": DB_SCHEMA,
            "ENTERPRISE_SYNC_SERVICE_TOKEN": SERVICE_TOKEN,
            "JWT_ISSUER": JWT_ISSUER,
            "JWT_AUDIENCE": JWT_AUDIENCE,
            "JWT_ENABLE_HS": "true",
            "JWT_ALLOWED_ALGS": "HS256",
            "JWT_JWKS_URL": "",
            "JWT_SHARED_SECRET": JWT_SECRET,
            "RAGFLOW_API_KEY": API_KEY,
            "RAGFLOW_BASE_URL": RAGFLOW_BASE_URL,
            "ENTERPRISE_TEST_MODE": "",
            "ENTERPRISE_QUERY_TRACE_DOC_IDS": str(QUERY_TRACE),
        }
    )
    return env


def _e2e_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_URL": GATEWAY_URL,
            "RAGFLOW_BASE_URL": RAGFLOW_BASE_URL,
            "RAGFLOW_ADMIN_URL": RAGFLOW_ADMIN_URL,
            "RAGFLOW_ADMIN_EMAIL": RAGFLOW_ADMIN_EMAIL,
            "RAGFLOW_ADMIN_PASSWORD": RAGFLOW_ADMIN_PASSWORD,
            "RAGFLOW_API_KEY": API_KEY,
            "ENTERPRISE_SYNC_SERVICE_TOKEN": SERVICE_TOKEN,
            "JWT_SHARED_SECRET": JWT_SECRET,
            "JWT_ENABLE_HS": "true",
            "JWT_ALLOWED_ALGS": "HS256",
            "JWT_JWKS_URL": "",
            "JWT_ISSUER": JWT_ISSUER,
            "JWT_AUDIENCE": JWT_AUDIENCE,
            "ENTERPRISE_GATEWAY_DATABASE_URL": DATABASE_URL,
            "ENTERPRISE_GATEWAY_DATABASE_SCHEMA": DB_SCHEMA,
            "S3_ENDPOINT": S3_ENDPOINT,
            "S3_ACCESS_KEY": S3_ACCESS_KEY,
            "S3_SECRET_KEY": S3_SECRET_KEY,
            "S3_BUCKET": os.environ.get("S3_BUCKET", ""),
            "WP04_E2E_REPORT": REPORT_PATH,
            "WP04_QUERY_TRACE": str(QUERY_TRACE),
            "WP04_SAMPLE_PREPARATION_MODE": SAMPLE_PREPARATION_MODE,
            "WP04_SAMPLE_SHA256": SAMPLE_SHA256_JSON,
        }
    )
    return env


def _wait_gateway() -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{GATEWAY_URL}/enterprise/api/v1/health", timeout=3
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("gateway did not become healthy")


def main() -> int:
    global API_KEY, SAMPLE_PREPARATION_MODE, SAMPLE_SHA256_JSON
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _assert_clean_tracked_worktree()
    _log("wp04 phase2 e2e starting")
    import json

    sample_info = prepare_wp03_samples(
        generator=_run_wp03_sample_generator
    )
    SAMPLE_PREPARATION_MODE = sample_info["mode"]
    SAMPLE_SHA256_JSON = json.dumps(sample_info["sha256"], sort_keys=True)
    _log(f"wp04 sample preparation mode {SAMPLE_PREPARATION_MODE}")
    QUERY_TRACE.unlink(missing_ok=True)
    API_KEY = _resolve_api_key()
    _log(f"gateway port {GATEWAY_PORT}")

    gateway = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "enterprise" / "scripts" / "wp03" / "run_gateway_e2e.py"),
        ],
        cwd=str(ROOT),
        env=_gateway_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_gateway()
        _log("gateway healthy")
        with RUN_LOG.open("a", encoding="utf-8") as out:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "enterprise" / "scripts" / "wp04_phase2_e2e.py"),
                ],
                cwd=str(ROOT),
                env=_e2e_env(),
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
        _log(f"e2e exit {result.returncode}")
        return result.returncode
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - background diagnostics
        _log(f"wp04 phase2 e2e failed: {exc}")
        raise
