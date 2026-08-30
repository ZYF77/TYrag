"""Supplementary real E2E checks: ACL 403 and source-version isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt as pyjwt

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.dialect import fetchall

LOG_PATH = ROOT / "artifacts" / "wp03-phase2-e2e-checks.log"
REPORT_PATH = ROOT / "artifacts" / "wp03-phase2-e2e-checks.json"
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wp03-phase2-e2e-checks")

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5190").rstrip("/")
SERVICE_TOKEN = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "")
TENANT = "phase2-e2e"
SOURCE_SYSTEM = "DEMO"
USER2 = "p2-user-2"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "https://auth.example.com")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "tyrag-gateway")
DOC_PREFIX = os.environ.get("E2E_DOC_PREFIX", "P2R")


def jwt_for(subject: str) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "tenant": TENANT,
        "name": subject,
        "department": ["d10"],
        "roles": ["end_user"],
        "groups": ["maintenance"],
        "security_level": 2,
        "iat": now - 60,
        "exp": now + 3600,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    return pyjwt.encode(claims, JWT_SECRET, algorithm="HS256")


def get(path: str, token: str | None = None, params: dict | None = None) -> httpx.Response:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=30) as c:
        return c.get(f"{GATEWAY}{path}", params=params, headers=headers)


def get_service(path: str, params: dict | None = None) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.get(
            f"{GATEWAY}{path}",
            params=params,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )


def post(path: str, body: dict) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.post(
            f"{GATEWAY}{path}",
            json=body,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )


async def ensure_user2() -> None:
    gateway = GatewayDatabase.from_env()
    try:
        await gateway.initialize()
        repo = ExtUserMapRepo(gateway=gateway)
        await repo.insert_mapping(
            ExtUserMap(
                tenant_id=TENANT,
                business_subject=USER2,
                business_user_id=USER2,
                mapping_strategy="B",
            )
        )
    finally:
        await gateway.dispose()


def wait_quality(doc_id: str, version: str, timeout_seconds: int = 180) -> dict:
    token = jwt_for("p2-user")
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        resp = get(
            f"/enterprise/api/v1/documents/{doc_id}/quality",
            token=token,
            params={"source_system": SOURCE_SYSTEM, "source_version_id": version},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("evaluationState") in ("completed", "failed"):
                return last
        time.sleep(3)
    raise TimeoutError(f"quality for {doc_id} {version} did not finish: {last}")


def wait_status(doc_id: str, version: str, timeout_seconds: int = 300) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        resp = get_service(
            f"/enterprise/api/v1/documents/{doc_id}/status",
            params={"tenant_id": TENANT, "source_version_id": version, "refresh": "true"},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") in ("ready", "failed", "disabled"):
                return last
        time.sleep(5)
    raise TimeoutError(f"status for {doc_id} {version} did not finish: {last}")


async def main() -> int:
    if not (SERVICE_TOKEN and JWT_SECRET):
        raise RuntimeError("service token and JWT secret are required")
    await ensure_user2()
    token2 = jwt_for(USER2)
    acl_results = {}
    for label in ("A", "B", "C"):
        doc_id = f"{DOC_PREFIX}-{label}"
        resp = get(
            f"/enterprise/api/v1/documents/{doc_id}/quality",
            token=token2,
            params={"source_system": SOURCE_SYSTEM},
        )
        acl_results[doc_id] = {
            "status": resp.status_code,
            "code": resp.json().get("code") if resp.headers.get("content-type", "").startswith("application/json") else None,
        }
        logger.info("acl check %s status=%s", doc_id, resp.status_code)

    a_pdf = ROOT / "artifacts" / "wp03" / "samples" / "wp03-digital_text-001.pdf"
    content = a_pdf.read_bytes()
    doc_id = f"{DOC_PREFIX}-A"
    payload = {
        "eventId": f"p2-v2-{uuid.uuid4().hex[:12]}",
        "eventType": "upsert",
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": doc_id,
        "sourceVersionId": "v2",
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": "p2-a-v2.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": "wp03-phase2-e2e", "objectKey": f"phase2/{doc_id}-v2.pdf"},
        "metadata": {
            "schema_version": 1,
            "tenant_id": TENANT,
            "external_document_id": doc_id,
            "source_system": SOURCE_SYSTEM,
            "equipment_id": f"EQ-{doc_id}",
            "document_type": "manual",
            "document_version": "v2",
            "department_id": "dept-eng",
            "security_level": 2,
            "business_status": "active",
            "page_count": 3,
        },
        "batchId": f"phase2-checks-{uuid.uuid4().hex[:8]}",
    }
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://127.0.0.1:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", ""),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", ""),
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        s3.head_bucket(Bucket="wp03-phase2-e2e")
    except Exception:
        s3.create_bucket(Bucket="wp03-phase2-e2e")
    s3.put_object(
        Bucket="wp03-phase2-e2e",
        Key=f"phase2/{doc_id}-v2.pdf",
        Body=content,
        ContentType="application/pdf",
    )
    resp = post("/enterprise/api/v1/documents", payload)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"v2 submit failed: {resp.status_code} {resp.text[:300]}")
    sync = wait_status(doc_id, "v2")
    quality_v2 = wait_quality(doc_id, "v2")
    gateway = GatewayDatabase.from_env()
    try:
        async with gateway.transaction() as conn:
            evals = await fetchall(
                conn,
                """SELECT evaluation_version, evaluation_state, parse_quality_status
                   FROM parse_quality_evaluation
                   WHERE tenant_id=? AND source_system=? AND external_document_id=?
                   ORDER BY evaluation_version""",
                (TENANT, SOURCE_SYSTEM, doc_id),
            )
    finally:
        await gateway.dispose()

    report = {
        "acl": acl_results,
        "version_isolation": {
            "sync_status": sync.get("status"),
            "quality_v2": quality_v2,
            "evaluations": evals,
        },
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (
        all(v["status"] == 403 for v in acl_results.values())
        and quality_v2.get("evaluationState") == "completed"
        and quality_v2.get("parseQualityStatus") == "passed"
        and len(evals) >= 2
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
