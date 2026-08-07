"""Real WP-03 Phase 2 E2E: A/B/C documents through gateway quality gate.

Requires the gateway started with quality worker enabled, plus real RAGFlow
and MinIO. Secrets are read from environment variables and never printed.
"""

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

import boto3
import httpx
import jwt as pyjwt
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.query import acl_store  # noqa: E402
from enterprise.gateway.sync.models import init_db  # noqa: E402

LOG_PATH = ROOT / "artifacts" / "wp03-phase2-e2e.log"
REPORT_PATH = ROOT / "artifacts" / "wp03-phase2-e2e-report.json"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("wp03-phase2-e2e")

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5190").rstrip("/")
RAGFLOW_URL = os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380")
SERVICE_TOKEN = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "")
DB_PATH = os.environ.get("ENTERPRISE_SYNC_DB_PATH", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://127.0.0.1:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "wp03-phase2-e2e")

TENANT = "phase2-e2e"
SOURCE_SYSTEM = "DEMO"
USER = "p2-user"
DOC_PREFIX = os.environ.get("E2E_DOC_PREFIX", "P2")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "https://auth.example.com")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "tyrag-gateway")


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def jwt_for(subject: str = USER) -> str:
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


def service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def user_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {jwt_for()}"}


def metadata_for(doc_id: str, page_count: int) -> dict:
    return {
        "schema_version": 1,
        "tenant_id": TENANT,
        "external_document_id": doc_id,
        "source_system": SOURCE_SYSTEM,
        "equipment_id": f"EQ-{doc_id}",
        "document_type": "manual",
        "document_version": "v1",
        "department_id": "dept-eng",
        "security_level": 2,
        "business_status": "active",
        "page_count": page_count,
    }


def payload_for(doc_id: str, file_name: str, content: bytes, page_count: int) -> dict:
    return {
        "eventId": f"p2-{doc_id}-{uuid.uuid4().hex[:12]}",
        "eventType": "upsert",
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": doc_id,
        "sourceVersionId": "v1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": file_name,
        "mediaType": "application/pdf",
        "source": {"bucket": S3_BUCKET, "objectKey": f"phase2/{doc_id}.pdf"},
        "metadata": metadata_for(doc_id, page_count),
        "batchId": f"phase2-e2e-{uuid.uuid4().hex[:8]}",
    }


def put_object(doc_id: str, content: bytes, content_type: str = "application/pdf") -> None:
    client = s3_client()
    try:
        client.head_bucket(Bucket=S3_BUCKET)
    except Exception:
        client.create_bucket(Bucket=S3_BUCKET)
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"phase2/{doc_id}.pdf",
        Body=content,
        ContentType=content_type,
    )


def gateway_post(path: str, json_body: dict) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.post(
            f"{GATEWAY}{path}",
            json=json_body,
            headers=service_headers(),
        )


def gateway_get(path: str, headers: dict[str, str], params: dict | None = None) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.get(f"{GATEWAY}{path}", params=params, headers=headers)


def wait_for_status(doc_id: str, timeout_seconds: int = 300) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        resp = gateway_get(
            f"/enterprise/api/v1/documents/{doc_id}/status",
            headers=service_headers(),
            params={"tenant_id": TENANT, "refresh": "true"},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") in ("ready", "failed", "disabled"):
                return last
        time.sleep(5)
    raise TimeoutError(f"document {doc_id} did not reach terminal status: {last}")


def wait_for_quality(doc_id: str, timeout_seconds: int = 180) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        resp = gateway_get(
            f"/enterprise/api/v1/documents/{doc_id}/quality",
            headers=user_headers(),
            params={"source_system": SOURCE_SYSTEM},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("evaluationState") in ("completed", "failed"):
                return last
        time.sleep(3)
    raise TimeoutError(f"quality for {doc_id} did not finish: {last}")


async def ensure_user_and_acl() -> None:
    db = await init_db(DB_PATH)
    repo = ExtUserMapRepo(db_path=DB_PATH)
    await repo.ensure_table()
    await repo.insert_mapping(
        ExtUserMap(
            tenant_id=TENANT,
            business_subject=USER,
            business_user_id=USER,
            mapping_strategy="B",
        )
    )
    await repo.close()
    await acl_store.ensure_schema(db)
    for label in ("A", "B", "C"):
        doc_id = f"{DOC_PREFIX}-{label}"
        await acl_store.grant(
            db,
            tenant_id=TENANT,
            external_document_id=doc_id,
            business_user_id=USER,
        )
    await db.close()


def ask(doc_id: str, question: str) -> tuple[int, dict]:
    with httpx.Client(timeout=60) as c:
        resp = c.post(
            f"{GATEWAY}/enterprise/api/v1/demo/ask",
            json={"externalDocumentId": doc_id, "question": question},
            headers=user_headers(),
        )
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {}


async def run_one(
    doc_id: str,
    file_name: str,
    content: bytes,
    page_count: int,
    media_type: str = "application/pdf",
) -> dict:
    logger.info("starting %s", doc_id)
    put_object(doc_id, content, media_type)
    payload = payload_for(doc_id, file_name, content, page_count)
    payload["mediaType"] = media_type
    resp = gateway_post("/enterprise/api/v1/documents", payload)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"submit failed for {doc_id}: {resp.status_code} {resp.text[:300]}")
    sync = wait_for_status(doc_id)
    quality = wait_for_quality(doc_id)
    ask_status, ask_body = ask(doc_id, "请给出故障码 E-104 的检查步骤")
    logger.info(
        "%s sync=%s quality=%s ask=%s",
        doc_id,
        sync.get("status"),
        quality.get("parseQualityStatus"),
        ask_status,
    )
    return {
        "doc_id": doc_id,
        "sync_status": sync.get("status"),
        "evaluation_state": quality.get("evaluationState"),
        "quality_status": quality.get("parseQualityStatus"),
        "quality_reasons": quality.get("qualityReasons", []),
        "ask_status": ask_status,
        "ask_code": ask_body.get("code") if isinstance(ask_body, dict) else None,
        "citations": (
            ask_body.get("citations", [])
            if isinstance(ask_body, dict) and ask_status == 200
            else []
        ),
    }


async def main() -> int:
    if not (SERVICE_TOKEN and JWT_SECRET and DB_PATH):
        raise RuntimeError("service token, JWT secret and DB path are required")
    await ensure_user_and_acl()
    samples = ROOT / "artifacts" / "wp03" / "samples"
    a_pdf = samples / "wp03-digital_text-001.pdf"
    b_pdf = samples / "wp03-degraded_scan-014.pdf"
    if not a_pdf.exists() or not b_pdf.exists():
        raise RuntimeError("sample PDFs missing; run generate_samples.py first")
    review_fixture = b"%PDF-1.7\n% review required fixture for phase2 e2e\n" + b"0" * 1024
    corrupt = b"this is not a pdf fixture for phase2 e2e\n" * 64
    results = {
        "A": await run_one(f"{DOC_PREFIX}-A", "p2-a.pdf", a_pdf.read_bytes(), 3),
        "B": await run_one(f"{DOC_PREFIX}-B", "p2-b.pdf", review_fixture, 1),
        "C": await run_one(
            f"{DOC_PREFIX}-C",
            "p2-c.xyz",
            corrupt,
            1,
            media_type="application/octet-stream",
        ),
    }
    report = {
        "gateway": GATEWAY,
        "ragflow": RAGFLOW_URL,
        "tenant": TENANT,
        "source_system": SOURCE_SYSTEM,
        "documents": results,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    a = results["A"]
    b = results["B"]
    c = results["C"]
    ok = (
        a["quality_status"] == "passed"
        and a["ask_status"] == 200
        and b["quality_status"] == "review_required"
        and b["ask_status"] == 409
        and b["ask_code"] == "DOCUMENT_REVIEW_REQUIRED"
        and c["quality_status"] == "failed"
        and c["ask_status"] == 409
        and c["ask_code"] in ("DOCUMENT_QUALITY_FAILED", "DOCUMENT_QUALITY_PENDING")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
