"""Real WP-04 E2E: MinIO -> formal WP-02 sync -> RAGFlow -> user ask.

Requires a running Enterprise Gateway plus real RAGFlow and MinIO. Secrets are
read from environment variables and never printed by this script.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from Cryptodome.Cipher import PKCS1_v1_5  # noqa: E402
from Cryptodome.PublicKey import RSA  # noqa: E402

from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.query.ragflow_client import (  # noqa: E402
    RAGFlowQueryClient,
)
from enterprise.gateway.sync.models import init_db  # noqa: E402

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188").rstrip("/")
RAGFLOW_URL = os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380").rstrip("/")
REPORT_PATH = Path(
    os.environ.get(
        "WP04_E2E_REPORT",
        str(ROOT / "artifacts" / "wp04-e2e-current.json"),
    )
)
ADMIN_URL = os.environ.get("RAGFLOW_ADMIN_URL", "http://127.0.0.1:9381").rstrip("/")
ADMIN_EMAIL = os.environ.get("RAGFLOW_ADMIN_EMAIL", "admin@ragflow.io")
ADMIN_PASSWORD = os.environ.get("RAGFLOW_ADMIN_PASSWORD", "")
API_KEY = os.environ.get("RAGFLOW_API_KEY", "").strip()
SERVICE_TOKEN = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "")
DB_PATH = os.environ.get("ENTERPRISE_SYNC_DB_PATH", "")

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

TENANT = os.environ.get("E2E_TENANT", "wp04e2e")
SOURCE_SYSTEM = "DEMO"
SOURCE_VERSION = "v1"
USER_A = os.environ.get("E2E_USER_A", "demo-user")
USER_B = os.environ.get("E2E_USER_B", "demo-user-2")
USER_C = os.environ.get("E2E_USER_C", "demo-user-readonly")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "https://auth.example.com")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "tyrag-gateway")
A_SAMPLE = Path(
    os.environ.get(
        "E2E_A_SAMPLE",
        str(ROOT / "artifacts" / "wp03" / "samples" / "wp03-digital_text-001.pdf"),
    )
)
B_SAMPLE = Path(
    os.environ.get(
        "E2E_B_SAMPLE",
        str(ROOT / "artifacts" / "wp03" / "samples" / "wp03-digital_text-002.pdf"),
    )
)


PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/
z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp2Q
2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOOUEfa
S318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVKRNLJpL8
w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK6olN4RevS
fVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs2wIDAQAB
-----END PUBLIC KEY-----"""


def _encrypt_password(password: str) -> str:
    rsa_key = RSA.importKey(PUBLIC_KEY)
    cipher = PKCS1_v1_5.new(rsa_key)
    plain = base64.b64encode(password.encode("utf-8")).decode("utf-8")
    return base64.b64encode(cipher.encrypt(plain.encode("utf-8"))).decode("utf-8")


def admin_token() -> str:
    body = {
        "email": ADMIN_EMAIL,
        "password": _encrypt_password(ADMIN_PASSWORD),
    }
    resp = httpx.post(
        f"{ADMIN_URL}/api/v1/admin/login",
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.headers.get("Authorization", "")
    if not token:
        raise RuntimeError("admin login returned no Authorization header")
    return token


def fetch_api_key(token: str, email: str) -> str:
    global API_KEY
    if API_KEY:
        return API_KEY
    from urllib.parse import quote

    resp = httpx.get(
        f"{ADMIN_URL}/api/v1/admin/users/{quote(email)}/keys",
        headers={"Authorization": token},
        timeout=10,
    )
    resp.raise_for_status()
    keys = resp.json().get("data") or []
    if keys:
        API_KEY = keys[0]["token"]
        return API_KEY
    created = httpx.post(
        f"{ADMIN_URL}/api/v1/admin/users/{quote(email)}/keys",
        headers={"Authorization": token},
        timeout=10,
    )
    created.raise_for_status()
    API_KEY = created.json()["data"]["token"]
    return API_KEY


def git_commit() -> str:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def ragflow_version(key: str) -> str:
    try:
        resp = httpx.get(
            f"{RAGFLOW_URL}/api/v1/system/version",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        resp.raise_for_status()
        version = str(resp.json().get("data") or "").strip()
        if version:
            return version
    except Exception:
        pass
    version_path = ROOT / "ragflow" / "VERSION"
    if version_path.exists():
        return version_path.read_text(encoding="utf-8").strip()
    return "unknown"


def jwt_for(
    subject: str,
    tenant: str = TENANT,
    roles: tuple[str, ...] = ("end_user",),
) -> str:
    import jwt as pyjwt

    now = int(time.time())
    claims = {
        "sub": subject,
        "tenant": tenant,
        "name": subject,
        "department": ["d10"],
        "roles": list(roles),
        "groups": ["maintenance"],
        "security_level": 2,
        "iat": now - 60,
        "exp": now + 3600,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    return pyjwt.encode(claims, JWT_SECRET, algorithm="HS256")


def s3_client():
    import boto3
    from botocore.config import Config

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


def s3_put(bucket: str, key: str, content: bytes) -> None:
    import io

    s3_client().upload_fileobj(
        io.BytesIO(content),
        bucket,
        key,
        ExtraArgs={"ContentType": "application/pdf"},
    )


def set_chat_top_n(chat_id: str, top_n: int, key: str) -> None:
    resp = httpx.patch(
        f"{RAGFLOW_URL}/api/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {key}"},
        json={"top_n": top_n},
        timeout=15,
    )
    resp.raise_for_status()


def gateway_post(path: str, *, headers: dict, json_body: dict | None = None,
                 data: bytes | None = None) -> httpx.Response:
    return httpx.post(
        f"{GATEWAY}{path}",
        headers=headers,
        json=json_body,
        content=data,
        timeout=180,
    )


def gateway_get(path: str, headers: dict, params: dict | None = None) -> httpx.Response:
    return httpx.get(
        f"{GATEWAY}{path}",
        headers=headers,
        params=params,
        timeout=60,
    )


def sync_payload(
    doc_id: str,
    event_id: str,
    key: str,
    bucket: str,
    content: bytes,
    page_count: int,
) -> dict:
    return {
        "eventId": event_id,
        "eventType": "upsert",
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": f"E2E-{doc_id}",
        "sourceVersionId": SOURCE_VERSION,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": f"{doc_id}.pdf",
        "mediaType": "application/pdf",
        "source": {"bucket": bucket, "objectKey": f"{key}.pdf"},
        "metadata": {
            "schema_version": 1,
            "tenant_id": TENANT,
            "external_document_id": f"E2E-{doc_id}",
            "source_system": SOURCE_SYSTEM,
            "equipment_id": "EQ-E2E-001",
            "fixed_asset_no": f"FA-{doc_id}",
            "page_count": page_count,
            "document_type": "manual",
            "document_version": SOURCE_VERSION,
            "department_id": "d10",
            "security_level": 2,
            "business_status": "active",
        },
        "batchId": None,
    }


async def ensure_users(db_path: str) -> None:
    repo = ExtUserMapRepo(db_path=db_path)
    await repo.ensure_table()
    for user in (USER_A, USER_B, USER_C):
        await repo.insert_mapping(
            ExtUserMap(
                tenant_id=TENANT,
                business_subject=user,
                business_user_id=user,
                mapping_strategy="B",
            )
        )
    await repo.close()


async def ensure_db_schema(db_path: str) -> None:
    db = await init_db(db_path)
    await db.close()


def grant_acl(db_path: str, external_document_id: str, user: str) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS demo_document_acl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                external_document_id TEXT NOT NULL,
                business_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, external_document_id, business_user_id)
            )"""
        )
        conn.execute(
            """INSERT INTO demo_document_acl
               (tenant_id, external_document_id, business_user_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tenant_id, external_document_id, business_user_id)
               DO NOTHING""",
            (
                TENANT,
                external_document_id,
                user,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def wait_ready(doc_id: str, timeout_seconds: int = 600) -> dict:
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        resp = gateway_get(
            f"/enterprise/api/v1/documents/{doc_id}/status",
            headers,
            params={
                "tenant_id": TENANT,
                "source_system": SOURCE_SYSTEM,
                "refresh": "true",
            },
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") == "ready":
                return last
        time.sleep(5)
    raise TimeoutError(f"document {doc_id} did not become ready: {last}")


def wait_quality(doc_id: str, headers: dict, timeout_seconds: int = 300) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        resp = gateway_get(
            f"/enterprise/api/v1/documents/{doc_id}/quality",
            headers,
            params={"source_system": SOURCE_SYSTEM},
        )
        if resp.status_code == 200:
            last = resp.json()
            if last.get("evaluationState") in ("completed", "failed"):
                return last
        time.sleep(3)
    raise TimeoutError(f"quality for {doc_id} did not finish: {last}")


def main() -> int:
    if not SERVICE_TOKEN or not JWT_SECRET or not DB_PATH:
        raise RuntimeError(
            "ENTERPRISE_SYNC_SERVICE_TOKEN, JWT_SHARED_SECRET and "
            "ENTERPRISE_SYNC_DB_PATH are required"
        )
    if not (S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY):
        raise RuntimeError("S3 endpoint/credentials are required")

    import asyncio

    asyncio.run(ensure_db_schema(DB_PATH))
    asyncio.run(ensure_users(DB_PATH))

    if API_KEY:
        key = API_KEY
    else:
        if not (ADMIN_EMAIL and ADMIN_PASSWORD):
            raise RuntimeError(
                "RAGFLOW_API_KEY, or RAGFLOW_ADMIN_EMAIL and "
                "RAGFLOW_ADMIN_PASSWORD, are required"
            )
        token = admin_token()
        key = fetch_api_key(token, ADMIN_EMAIL)
    print(f"ragflow api key: {key[:14]}... (masked)")
    commit = git_commit()
    ragflow_version_value = ragflow_version(key)
    test_time = datetime.now(timezone.utc).isoformat()

    bucket = S3_BUCKET or f"wp04-e2e-{uuid.uuid4().hex[:8]}"
    if S3_BUCKET:
        try:
            s3_client().head_bucket(Bucket=bucket)
        except Exception:
            s3_client().create_bucket(Bucket=bucket)
    else:
        s3_client().create_bucket(Bucket=bucket)

    if not A_SAMPLE.exists() or not B_SAMPLE.exists():
        raise RuntimeError("E2E sample PDFs missing; run wp03 generate_samples.py first")
    content_a = A_SAMPLE.read_bytes()
    content_b = B_SAMPLE.read_bytes()
    s3_put(bucket, "DocA.pdf", content_a)
    s3_put(bucket, "DocB.pdf", content_b)

    service_headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    user_a_token = jwt_for(USER_A)
    user_b_token = jwt_for(USER_B)
    user_c_token = jwt_for(USER_C, roles=())
    user_a_headers = {"Authorization": f"Bearer {user_a_token}"}
    user_b_headers = {"Authorization": f"Bearer {user_b_token}"}
    user_c_headers = {"Authorization": f"Bearer {user_c_token}"}

    event_a = f"wp04-{uuid.uuid4().hex[:16]}"
    payload_a = sync_payload(
        "Doc1", event_a, "DocA", bucket, content_a, 3
    )
    resp = gateway_post(
        "/enterprise/api/v1/documents",
        headers=service_headers,
        json_body=payload_a,
    )
    assert resp.status_code == 202, resp.text
    sync_a = resp.json()
    doc_a_id = sync_a["externalDocumentId"]

    event_b = f"wp04-{uuid.uuid4().hex[:16]}"
    payload_b = sync_payload(
        "Doc2", event_b, "DocB", bucket, content_b, 4
    )
    resp = gateway_post(
        "/enterprise/api/v1/documents",
        headers=service_headers,
        json_body=payload_b,
    )
    assert resp.status_code == 202, resp.text
    sync_b = resp.json()
    doc_b_id = sync_b["externalDocumentId"]

    resp = gateway_post(
        "/enterprise/api/v1/documents",
        headers=service_headers,
        json_body=payload_a,
    )
    assert resp.status_code == 202, resp.text
    dedup = resp.json()
    assert dedup["deduplicated"] is True, dedup

    grant_acl(DB_PATH, doc_a_id, USER_A)
    grant_acl(DB_PATH, doc_b_id, USER_B)
    grant_acl(DB_PATH, doc_a_id, USER_C)

    blocked = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_a_headers,
        json_body={"externalDocumentId": doc_a_id, "question": "提前提问"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] in (
        "DOCUMENT_NOT_READY",
        "DOCUMENT_QUALITY_PENDING",
    ), blocked.json()

    ready_a = wait_ready(doc_a_id)
    ready_b = wait_ready(doc_b_id)
    assert ready_a["status"] == "ready"
    assert ready_b["status"] == "ready"
    quality_a = wait_quality(doc_a_id, user_a_headers)
    quality_b = wait_quality(doc_b_id, user_b_headers)
    assert quality_a["parseQualityStatus"] == "passed", quality_a
    assert quality_b["parseQualityStatus"] == "passed", quality_b

    first = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_a_headers,
        json_body={
            "externalDocumentId": doc_a_id,
            "question": "What equipment id is listed in this document?",
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["answer"]
    citations = first_body["citations"]
    assert citations, "ask returned no citations"
    assert all(
        c["documentId"] == ready_a["ragflowDocumentId"] for c in citations
    ), citations
    for c in citations:
        assert c["documentId"] and c["title"]
        assert c["versionId"] == SOURCE_VERSION
        assert c["assetId"] == "FA-Doc1"
        assert c["pageNo"] is not None or c["bbox"] is not None

    conversation_id = first_body["conversationId"]
    session_first = first_body["ragflowSessionId"]
    client = RAGFlowQueryClient(api_key=key)
    chats_a = asyncio.run(
        client.list_chats(
            name=f"enterprise-demo-{ready_a['ragflowDatasetId']}"
        )
    )
    assert chats_a, "chat not found for dataset A"
    chat_a_id = chats_a[0]["id"]

    second = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_a_headers,
        json_body={
            "externalDocumentId": doc_a_id,
            "conversationId": conversation_id,
            "question": "What fixed asset number is listed in this document?",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["ragflowSessionId"] == session_first

    set_chat_top_n(chat_a_id, 0, key)
    try:
        no_evidence = gateway_post(
            "/enterprise/api/v1/demo/ask",
            headers=user_a_headers,
            json_body={
                "externalDocumentId": doc_a_id,
                "question": (
                    "Answer this question which is unrelated to the document: "
                    f"{uuid.uuid4().hex}"
                ),
            },
        )
    finally:
        set_chat_top_n(chat_a_id, 6, key)
    assert no_evidence.status_code == 200, no_evidence.text
    no_evidence_body = no_evidence.json()
    assert no_evidence_body["code"] == "NO_RELIABLE_EVIDENCE", (
        no_evidence.text
    )
    assert no_evidence_body["answer"]
    assert no_evidence_body["citations"] == []
    no_evidence_conversation_id = no_evidence_body["conversationId"]

    no_evidence_history = gateway_get(
        f"/enterprise/api/v1/demo/conversations/{no_evidence_conversation_id}",
        user_a_headers,
    )
    assert no_evidence_history.status_code == 200, no_evidence_history.text
    no_evidence_messages = no_evidence_history.json()["messages"]
    no_evidence_assistant = next(
        m
        for m in no_evidence_messages
        if m["role"] == "assistant"
        and m["status"] == "no_reliable_evidence"
    )
    assert no_evidence_assistant["content"].strip()
    assert no_evidence_assistant["citations"] == []

    history = gateway_get(
        f"/enterprise/api/v1/demo/conversations/{conversation_id}",
        user_a_headers,
    )
    assert history.status_code == 200, history.text
    messages = history.json()["messages"]
    assert len(messages) >= 4, messages

    denied_a = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_b_headers,
        json_body={"externalDocumentId": doc_a_id, "question": "hello"},
    )
    assert denied_a.status_code == 403, denied_a.text
    assert denied_a.json()["code"] == "ACL_DENIED"

    denied_b = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_a_headers,
        json_body={"externalDocumentId": doc_b_id, "question": "hello"},
    )
    assert denied_b.status_code == 403, denied_b.text
    assert denied_b.json()["code"] == "ACL_DENIED"

    denied_capability = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_c_headers,
        json_body={"externalDocumentId": doc_a_id, "question": "hello"},
    )
    assert denied_capability.status_code == 403, denied_capability.text
    assert denied_capability.json()["code"] == "ACL_DENIED"

    allowed_b = gateway_post(
        "/enterprise/api/v1/demo/ask",
        headers=user_b_headers,
        json_body={
            "externalDocumentId": doc_b_id,
            "question": "What equipment id is listed in this document?",
        },
    )
    assert allowed_b.status_code == 200, allowed_b.text
    b_citations = allowed_b.json()["citations"]
    assert b_citations, "user B ask returned no citations"
    assert all(
        c["documentId"] == ready_b["ragflowDocumentId"] for c in b_citations
    ), b_citations
    assert all(
        c["versionId"] == SOURCE_VERSION and c["assetId"] == "FA-Doc2"
        for c in b_citations
    ), b_citations

    chats = asyncio.run(
        client.list_chats(name=f"enterprise-demo-{ready_b['ragflowDatasetId']}")
    )
    assert chats, "chat not found for dataset B"
    chat_id = chats[0]["id"]
    completion = asyncio.run(
        client.chat_completion(
            chat_id,
            "What is RAGFlow?",
            doc_ids=[ready_b["ragflowDocumentId"]],
        )
    )
    chunks = (completion.get("data", {}).get("reference") or {}).get("chunks") or []
    assert all(
        c.get("document_id") == ready_b["ragflowDocumentId"] for c in chunks
    ), "RAGFlow doc_ids scope leaked unauthorized chunks"

    evidence = {
        "gitCommit": commit,
        "ragflowVersion": ragflow_version_value,
        "testTime": test_time,
        "documentA": {
            "externalDocumentId": doc_a_id,
            "eventId": event_a,
            "ragflowDatasetId": ready_a["ragflowDatasetId"],
            "ragflowDocumentId": ready_a["ragflowDocumentId"],
            "status": ready_a["status"],
            "qualityStatus": quality_a["parseQualityStatus"],
        },
        "documentB": {
            "externalDocumentId": doc_b_id,
            "eventId": event_b,
            "ragflowDatasetId": ready_b["ragflowDatasetId"],
            "ragflowDocumentId": ready_b["ragflowDocumentId"],
            "status": ready_b["status"],
            "qualityStatus": quality_b["parseQualityStatus"],
        },
        "idempotentReplayDeduplicated": dedup["deduplicated"],
        "notReadyRequestStatus": blocked.status_code,
        "notReadyRequestCode": blocked.json()["code"],
        "askAStatus": first.status_code,
        "askBStatus": allowed_b.status_code,
        "answerBusinessStatus": "completed",
        "noEvidenceRequestStatus": no_evidence.status_code,
        "noEvidenceCode": no_evidence_body["code"],
        "noEvidenceConversationId": no_evidence_conversation_id,
        "noEvidenceDocumentId": doc_a_id,
        "noEvidenceTrigger": "chat_top_n=0",
        "noEvidenceHistoryStatus": no_evidence_assistant["status"],
        "noEvidenceHistoryCitationCount": len(
            no_evidence_assistant["citations"]
        ),
        "noEvidenceHistoryContentPresent": bool(
            no_evidence_assistant["content"].strip()
        ),
        "userAAnswerPresent": bool(first_body["answer"]),
        "qualityAPassed": quality_a["parseQualityStatus"] == "passed",
        "qualityBPassed": quality_b["parseQualityStatus"] == "passed",
        "userACitationCount": len(citations),
        "userBCitationCount": len(b_citations),
        "userACitationsDocumentIds": sorted(
            {c["documentId"] for c in citations}
        ),
        "citationHasIdAndTitle": True,
        "citationHasPageOrBbox": True,
        "citationVersionIds": sorted(
            {c["versionId"] for c in citations}
        ),
        "citationAssetIds": sorted(
            {c["assetId"] for c in citations}
        ),
        "citationVersionMapped": all(
            c["versionId"] == SOURCE_VERSION for c in citations
        ),
        "citationAssetMapped": all(
            c["assetId"] == "FA-Doc1" for c in citations
        ),
        "sessionReused": second.json()["ragflowSessionId"] == session_first,
        "conversationMessageCount": len(messages),
        "userBDeniedOnDocAStatus": denied_a.status_code,
        "userADeniedOnDocBStatus": denied_b.status_code,
        "readOnlyCapabilityDeniedStatus": denied_capability.status_code,
        "userBCitationsDocumentIds": sorted(
            {c["documentId"] for c in b_citations}
        ),
        "ragflowDocIdsScopeLeakFree": True,
        "s3Bucket": bucket,
    }
    REPORT_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
