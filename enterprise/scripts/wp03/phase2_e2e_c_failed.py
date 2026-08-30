"""Real E2E for the failed-quality document fixture."""

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
from enterprise.gateway.db.database import GatewayDatabase

LOG_PATH = ROOT / "artifacts" / "wp03-phase2-e2e-c-failed.log"
REPORT_PATH = ROOT / "artifacts" / "wp03-phase2-e2e-c-failed.json"
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wp03-phase2-e2e-c")

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5190").rstrip("/")
SERVICE_TOKEN = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "")
TENANT = "phase2-e2e"
SOURCE_SYSTEM = "DEMO"
USER = "p2-user"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "https://auth.example.com")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "tyrag-gateway")
DOC_ID = "P2FC-C3"


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


def post(path: str, body: dict) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.post(
            f"{GATEWAY}{path}",
            json=body,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )


def get(path: str, headers: dict, params: dict | None = None) -> httpx.Response:
    with httpx.Client(timeout=30) as c:
        return c.get(f"{GATEWAY}{path}", params=params, headers=headers)


async def grant_acl() -> None:
    gateway = GatewayDatabase.from_env()
    try:
        await gateway.initialize()
        async with gateway.transaction(write=True) as conn:
            await acl_store.grant(
                conn,
                tenant_id=TENANT,
                external_document_id=DOC_ID,
                business_user_id=USER,
            )
    finally:
        await gateway.dispose()


def wait_status(timeout_seconds: int = 120) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    service = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    while time.time() < deadline:
        resp = get(
            f"/enterprise/api/v1/documents/{DOC_ID}/status",
            service,
            {"tenant_id": TENANT, "refresh": "true"},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") in ("ready", "failed", "disabled"):
                return last
        time.sleep(3)
    raise TimeoutError(f"status did not finish: {last}")


def wait_quality(timeout_seconds: int = 90) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    user = {"Authorization": f"Bearer {jwt_for()}"}
    while time.time() < deadline:
        resp = get(
            f"/enterprise/api/v1/documents/{DOC_ID}/quality",
            user,
            {"source_system": SOURCE_SYSTEM},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("evaluationState") in ("completed", "failed"):
                return last
        time.sleep(3)
    raise TimeoutError(f"quality did not finish: {last}")


def ask() -> tuple[int, dict]:
    with httpx.Client(timeout=30) as c:
        resp = c.post(
            f"{GATEWAY}/enterprise/api/v1/demo/ask",
            json={"externalDocumentId": DOC_ID, "question": "检查步骤"},
            headers={"Authorization": f"Bearer {jwt_for()}"},
        )
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {}


async def main() -> int:
    if not (SERVICE_TOKEN and JWT_SECRET):
        raise RuntimeError("service token and JWT secret are required")
    await grant_acl()
    content = b"this is not a pdf fixture for phase2 e2e\n" * 64
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
        Key=f"phase2/{DOC_ID}.xyz",
        Body=content,
        ContentType="application/octet-stream",
    )
    payload = {
        "eventId": f"p2fc-{uuid.uuid4().hex[:12]}",
        "eventType": "upsert",
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": DOC_ID,
        "sourceVersionId": "v1",
        "sha256": "0" * 64,
        "fileName": "p2-c.xyz",
        "mediaType": "application/octet-stream",
        "source": {"bucket": "wp03-phase2-e2e", "objectKey": f"phase2/{DOC_ID}.xyz"},
        "metadata": {
            "schema_version": 1,
            "tenant_id": TENANT,
            "external_document_id": DOC_ID,
            "source_system": SOURCE_SYSTEM,
            "equipment_id": "EQ-C",
            "document_type": "manual",
            "document_version": "v1",
            "department_id": "dept-eng",
            "security_level": 2,
            "business_status": "active",
            "page_count": 1,
        },
        "batchId": "phase2-c-failed",
    }
    resp = post("/enterprise/api/v1/documents", payload)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"submit failed: {resp.status_code} {resp.text[:300]}")
    sync = wait_status()
    quality = wait_quality()
    ask_status, ask_body = ask()
    report = {
        "doc_id": DOC_ID,
        "sync_status": sync.get("status"),
        "evaluation_state": quality.get("evaluationState"),
        "quality_status": quality.get("parseQualityStatus"),
        "quality_reasons": quality.get("qualityReasons", []),
        "ask_status": ask_status,
        "ask_code": ask_body.get("code") if isinstance(ask_body, dict) else None,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("C report: %s", report)
    return 0 if (
        quality.get("quality_status") == "failed"
        and ask_status == 409
        and ask_body.get("code") in ("DOCUMENT_QUALITY_FAILED", "DOCUMENT_QUALITY_PENDING")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
