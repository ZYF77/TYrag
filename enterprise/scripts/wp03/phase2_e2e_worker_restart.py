"""Real E2E: pending quality jobs recover after gateway worker restart."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.quality.models import get_or_create_evaluation  # noqa: E402
from enterprise.gateway.quality.routing import route_document  # noqa: E402
from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.dialect import exec_sql, fetchone
from enterprise.gateway.sync.models import (  # noqa: E402
    ExtDocumentMap,
    insert_mapping,
    update_mapping_status,
)

LOG_PATH = ROOT / "artifacts" / "wp03-phase2-worker-restart.log"
REPORT_PATH = ROOT / "artifacts" / "wp03-phase2-worker-restart.json"
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wp03-phase2-worker-restart")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5190").rstrip("/")
PORT = int(os.environ.get("GATEWAY_PORT", "5190"))
PYTHON = os.environ.get("PYTHON_EXE", sys.executable)
TENANT = "phase2-e2e"
SOURCE_SYSTEM = "DEMO"
DOC_ID = "P2RESTART-A"


def port_pid() -> int | None:
    output = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    for line in output.splitlines():
        if f":{PORT}" in line and "LISTENING" in line:
            return int(line.strip().split()[-1])
    return None


def stop_gateway() -> None:
    pid = port_pid()
    if not pid:
        raise RuntimeError(f"gateway not listening on {PORT}")
    logger.info("stopping gateway pid=%s", pid)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
        check=True,
        timeout=30,
    )
    for _ in range(20):
        if port_pid() is None:
            return
        time.sleep(0.5)
    raise RuntimeError("gateway did not stop")


def start_gateway() -> None:
    runner = ROOT / "enterprise" / "scripts" / "wp03" / "run_gateway_e2e.py"
    env = dict(os.environ)
    env["GATEWAY_PORT"] = str(PORT)
    subprocess.Popen(
        [PYTHON, str(runner)],
        cwd=str(ROOT),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{GATEWAY_URL}/enterprise/api/v1/health", timeout=3)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("gateway did not become healthy after restart")


async def prepare_pending_job() -> int:
    gateway = GatewayDatabase.from_env()
    try:
        await gateway.initialize()
        async with gateway.transaction(write=True) as db:
            row = await fetchone(
                db,
                """SELECT * FROM ext_document_map
                   WHERE tenant_id=? AND source_system=? AND external_document_id=?
                     AND sync_status='ready' AND business_status='active'
                   ORDER BY id DESC LIMIT 1""",
                (TENANT, SOURCE_SYSTEM, f"{os.environ.get('E2E_DOC_PREFIX', 'P2R')}-A"),
            )
            if not row:
                raise RuntimeError("source document not found for restart fixture")
            doc = ExtDocumentMap(
                tenant_id=TENANT,
                source_system=SOURCE_SYSTEM,
                external_document_id=DOC_ID,
                source_version_id="v1",
                event_id=str(uuid.uuid4()),
                sha256=row["sha256"],
                file_name="restart.pdf",
                media_type=row["media_type"],
                source_page_count=row["source_page_count"],
                ragflow_dataset_id=row["ragflow_dataset_id"],
                ragflow_document_id=row["ragflow_document_id"],
                sync_status="ready",
            )
            doc = await insert_mapping(db, doc)
            await update_mapping_status(
                db, doc, "ready", pipeline_status="DONE",
                business_status="active", current_version=1,
            )
            routing = route_document(
                media_type=doc.media_type,
                file_name=doc.file_name,
                source_system=doc.source_system,
            )
            evaluation = await get_or_create_evaluation(
                db,
                tenant_id=doc.tenant_id,
                source_system=doc.source_system,
                external_document_id=doc.external_document_id,
                source_version_id=doc.source_version_id,
                ragflow_dataset_id=doc.ragflow_dataset_id,
                ragflow_document_id=doc.ragflow_document_id,
                routing=routing,
                evaluation_version="1",
            )
            await exec_sql(
                db,
                "UPDATE quality_evaluation_job SET next_retry_at=? "
                "WHERE evaluation_id=?",
                (
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    evaluation.id,
                ),
            )
        return evaluation.id
    finally:
        await gateway.dispose()


async def wait_completed(evaluation_id: int, timeout_seconds: int = 120) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        gateway = GatewayDatabase.from_env()
        try:
            async with gateway.transaction() as conn:
                row = await fetchone(
                    conn,
                    """SELECT evaluation_state, parse_quality_status
                       FROM parse_quality_evaluation WHERE id=?""",
                    (evaluation_id,),
                )
        finally:
            await gateway.dispose()
        if row:
            last = dict(row)
            if row["evaluation_state"] in ("completed", "failed"):
                return last
        await asyncio.sleep(3)
    raise TimeoutError(f"quality evaluation did not recover: {last}")


async def main() -> int:
    evaluation_id = await prepare_pending_job()
    stop_gateway()
    start_gateway()
    gateway = GatewayDatabase.from_env()
    try:
        async with gateway.transaction(write=True) as conn:
            await exec_sql(
                conn,
                "UPDATE quality_evaluation_job SET next_retry_at=NULL "
                "WHERE evaluation_id=?",
                (evaluation_id,),
            )
    finally:
        await gateway.dispose()
    result = await wait_completed(evaluation_id)
    report = {
        "doc_id": DOC_ID,
        "evaluation_id": evaluation_id,
        "recovered": result,
        "gateway_restarted": True,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.get("evaluation_state") == "completed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        logger.exception("worker restart E2E failed")
        raise
