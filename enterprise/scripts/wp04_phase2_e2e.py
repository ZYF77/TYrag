"""Real WP-04 Phase 2 E2E: formal conversation, SSE ask, citation snapshot.

Walks the production (non-demo) API: authentication -> create conversation ->
streaming ask -> RAGFlow -> answer -> citation -> history -> citation API.
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
        str(ROOT / "artifacts" / "wp04-phase2-e2e-current.json"),
    )
)
ADMIN_URL = os.environ.get("RAGFLOW_ADMIN_URL", "http://127.0.0.1:9381").rstrip("/")
ADMIN_EMAIL = os.environ.get("RAGFLOW_ADMIN_EMAIL", "admin@ragflow.io")
ADMIN_PASSWORD = os.environ.get("RAGFLOW_ADMIN_PASSWORD", "")
API_KEY = os.environ.get("RAGFLOW_API_KEY", "").strip()
SERVICE_TOKEN = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", "")
JWT_SECRET = os.environ.get("JWT_SHARED_SECRET", "")
DB_PATH = os.environ.get("ENTERPRISE_SYNC_DB_PATH", "")
QUERY_TRACE = os.environ.get(
    "WP04_QUERY_TRACE",
    str(ROOT / "artifacts" / "wp04-phase2-docids-trace.log"),
)

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


def worktree_dirty() -> bool:
    import subprocess

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
    )
    return bool(result.stdout.strip())


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
    groups: tuple[str, ...] = ("maintenance",),
) -> str:
    import jwt as pyjwt

    now = int(time.time())
    claims = {
        "sub": subject,
        "tenant": tenant,
        "name": subject,
        "department": ["d10"],
        "roles": list(roles),
        "groups": list(groups),
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


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event = "message"
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if data:
            events.append((event, json.loads(data)))
    return events


def sync_payload(
    doc_id: str,
    event_id: str,
    key: str,
    bucket: str,
    content: bytes,
    page_count: int,
    allow_groups: list[str] | None = None,
) -> dict:
    allow_groups = allow_groups or ["maintenance"]
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
            "allow_group_ids": allow_groups,
            "deny_group_ids": [],
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


def trace_entries(path: str) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def trace_doc_ids(path: str, start: int) -> list[str]:
    return sorted(
        {
            doc_id
            for entry in trace_entries(path)[start:]
            for doc_id in entry.get("docIds", [])
        }
    )


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


def _wait_url(url: str, timeout_seconds: int = 90) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=3)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"url did not become healthy: {url}")


def verify_transport_failure(user_headers: dict) -> dict:
    """Prove a fully unreachable RAGFlow maps to run.failed (pre-stream)."""
    import subprocess

    port = os.environ.get("WP04_TRANSPORT_GATEWAY_PORT", "5197")
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_PORT": port,
            "RAGFLOW_BASE_URL": "http://127.0.0.1:1",
            "ENTERPRISE_TEST_MODE": "",
            "ENTERPRISE_DEMO_ROUTES_ENABLED": "true",
            "ENTERPRISE_QUERY_TRACE_DOC_IDS": "",
        }
    )
    gateway = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "enterprise" / "scripts" / "wp03" / "run_gateway_e2e.py"),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_url(f"{base}/enterprise/api/v1/health")
        created = httpx.post(
            f"{base}/enterprise/api/v1/conversations",
            headers=user_headers,
            json={},
            timeout=30,
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["conversationId"]
        ask = httpx.post(
            f"{base}/enterprise/api/v1/conversations/"
            f"{conversation_id}/messages:stream",
            headers={**user_headers, "Accept": "text/event-stream"},
            params={"stream": "true"},
            json={"question": "transport failure probe"},
            timeout=60,
        )
        events = parse_sse(ask.text)
        assert any(event == "run.failed" for event, _ in events), ask.text
        assert not any(
            event == "answer.completed" for event, _ in events
        ), ask.text
        failed = next(
            data
            for event, data in events
            if event == "run.failed"
        )
        assert failed["code"] == "RAGFLOW_UNAVAILABLE", failed
        history = httpx.get(
            f"{base}/enterprise/api/v1/conversations/{conversation_id}",
            headers=user_headers,
            timeout=30,
        )
        assert history.status_code == 200, history.text
        assistant = next(
            m
            for m in history.json()["messages"]
            if m["role"] == "assistant"
        )
        assert assistant["status"] == "failed", history.text
        return {
            "transportFailureStatus": failed["code"],
            "transportFailureStage": "pre_stream_chat_lookup",
            "transportFailureHistoryStatus": assistant["status"],
        }
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway.kill()


def verify_stream_gateway_failure(
    user_headers: dict,
    dataset_id: str,
    expected_doc_ids: list[str],
    key: str,
) -> dict:
    """Prove a mid-stream RAGFlow disconnect maps to run.failed via SSE.

    The fault server answers /api/v1/chats so _ensure_chat succeeds, then
    closes /api/v1/chat/completions mid-stream. The Gateway therefore reaches
    chat_completion_stream() over a real socket.
    """
    import asyncio
    import subprocess

    from enterprise.scripts.wp04_stream_transport_probe import (
        StreamFaultServer,
        run_direct_probe,
    )

    async def _run() -> dict:
        direct_evidence = await run_direct_probe(key)
        async with StreamFaultServer(
            f"enterprise-formal-{TENANT}",
            dataset_ids=[dataset_id],
        ) as server:
            port = os.environ.get("WP04_STREAM_GATEWAY_PORT", "5198")
            base = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env.update(
                {
                    "GATEWAY_PORT": port,
                    "RAGFLOW_BASE_URL": server.base_url,
                    "ENTERPRISE_TEST_MODE": "",
                    "ENTERPRISE_DEMO_ROUTES_ENABLED": "true",
                    "ENTERPRISE_QUERY_TRACE_DOC_IDS": str(
                        ROOT / "artifacts" / "wp04-phase2-stream-trace.log"
                    ),
                }
            )
            gateway = subprocess.Popen(
                [
                    sys.executable,
                    str(
                        ROOT
                        / "enterprise"
                        / "scripts"
                        / "wp03"
                        / "run_gateway_e2e.py"
                    ),
                ],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _wait_url(f"{base}/enterprise/api/v1/health")
                created = httpx.post(
                    f"{base}/enterprise/api/v1/conversations",
                    headers=user_headers,
                    json={},
                    timeout=30,
                )
                assert created.status_code == 201, created.text
                conversation_id = created.json()["conversationId"]
                ask = httpx.post(
                    f"{base}/enterprise/api/v1/conversations/"
                    f"{conversation_id}/messages:stream",
                    headers={**user_headers, "Accept": "text/event-stream"},
                    params={"stream": "true"},
                    json={"question": "stream transport probe"},
                    timeout=60,
                )
                events = parse_sse(ask.text)
                assert any(
                    event == "run.failed" for event, _ in events
                ), ask.text
                assert not any(
                    event == "answer.completed" for event, _ in events
                ), ask.text
                failed = next(
                    data
                    for event, data in events
                    if event == "run.failed"
                )
                assert failed["code"] == "RAGFLOW_UNAVAILABLE", failed
                history = httpx.get(
                    f"{base}/enterprise/api/v1/conversations/"
                    f"{conversation_id}",
                    headers=user_headers,
                    timeout=30,
                )
                assert history.status_code == 200, history.text
                assistant = next(
                    m
                    for m in history.json()["messages"]
                    if m["role"] == "assistant"
                )
                assert assistant["status"] == "failed", history.text
                assert server.completion_requests == 1, (
                    "stream fault server did not receive chat completions"
                )
                sent_body = server.last_completion_body or {}
                raw_doc_ids = sent_body.get("doc_ids", "")
                sent_doc_ids = sorted(
                    str(raw_doc_ids).split(",")
                ) if raw_doc_ids else []
                assert sent_doc_ids == sorted(expected_doc_ids), sent_body
                return {
                    **direct_evidence,
                    "streamTransportGatewayRunFailed": True,
                    "streamTransportGatewayAnswerCompleted": False,
                    "streamTransportGatewayHistoryStatus": (
                        assistant["status"]
                    ),
                    "streamTransportDocIdsSent": sent_doc_ids,
                }
            finally:
                gateway.terminate()
                try:
                    gateway.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    gateway.kill()

    return asyncio.run(_run())


def main() -> int:
    if not SERVICE_TOKEN or not JWT_SECRET or not DB_PATH:
        raise RuntimeError(
            "ENTERPRISE_SYNC_SERVICE_TOKEN, JWT_SHARED_SECRET and "
            "ENTERPRISE_SYNC_DB_PATH are required"
        )
    if not (S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY):
        raise RuntimeError("S3 endpoint/credentials are required")
    if not QUERY_TRACE:
        raise RuntimeError("WP04_QUERY_TRACE must resolve to a trace path")
    trace_path = Path(QUERY_TRACE)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("", encoding="utf-8")

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
    user_a_token = jwt_for(USER_A, groups=("group-a",))
    user_b_token = jwt_for(USER_B, groups=("group-b",))
    user_c_token = jwt_for(USER_C, roles=(), groups=())
    user_a_headers = {"Authorization": f"Bearer {user_a_token}"}
    user_b_headers = {"Authorization": f"Bearer {user_b_token}"}
    user_c_headers = {"Authorization": f"Bearer {user_c_token}"}

    event_a = f"wp04-{uuid.uuid4().hex[:16]}"
    payload_a = sync_payload(
        "Doc1", event_a, "DocA", bucket, content_a, 3,
        allow_groups=["group-a"],
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
        "Doc2", event_b, "DocB", bucket, content_b, 4,
        allow_groups=["group-b"],
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

    blocked_conversation = gateway_post(
        "/enterprise/api/v1/conversations",
        headers=user_a_headers,
        json_body={},
    )
    assert blocked_conversation.status_code == 201, blocked_conversation.text
    blocked_conv_id = blocked_conversation.json()["conversationId"]
    blocked = gateway_post(
        f"/enterprise/api/v1/conversations/{blocked_conv_id}/messages:stream",
        headers=user_a_headers,
        json_body={"question": "提前提问"},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "no_reliable_evidence", blocked.text
    blocked_messages = gateway_get(
        f"/enterprise/api/v1/conversations/{blocked_conv_id}",
        user_a_headers,
    ).json()["messages"]
    assert any(
        m["status"] == "no_reliable_evidence" for m in blocked_messages
    ), blocked_messages

    ready_a = wait_ready(doc_a_id)
    ready_b = wait_ready(doc_b_id)
    assert ready_a["status"] == "ready"
    assert ready_b["status"] == "ready"
    quality_a = wait_quality(doc_a_id, user_a_headers)
    quality_b = wait_quality(doc_b_id, user_b_headers)
    assert quality_a["parseQualityStatus"] == "passed", quality_a
    assert quality_b["parseQualityStatus"] == "passed", quality_b

    created_a = gateway_post(
        "/enterprise/api/v1/conversations",
        headers=user_a_headers,
        json_body={"equipmentId": "EQ-E2E-001"},
    )
    assert created_a.status_code == 201, created_a.text
    conversation_id = created_a.json()["conversationId"]
    assert "ragflowSessionId" not in created_a.json()

    sse_headers = {**user_a_headers, "Accept": "text/event-stream"}
    trace_start_a = len(trace_entries(QUERY_TRACE))
    first = gateway_post(
        f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream?stream=true",
        headers=sse_headers,
        json_body={"question": "What equipment id is listed in this document?"},
    )
    assert first.status_code == 200, first.text
    first_events = parse_sse(first.text)
    assert first_events[0][0] == "run.started"
    first_completed = next(
        data for event, data in first_events if event == "answer.completed"
    )
    first_body = first_completed
    stream_answer_text = "".join(
        data["content"]
        for event, data in first_events
        if event == "answer.delta" and data.get("content")
    )
    assert first_body["status"] == "completed", first_body
    citations = first_body["citations"]
    assert citations, "formal ask returned no citations"
    assert all(
        c["documentId"] == doc_a_id for c in citations
    ), citations
    authorized_ragflow_doc_ids_sent = trace_doc_ids(
        QUERY_TRACE, trace_start_a
    )
    expected_authorized_ragflow_doc_ids = sorted(
        [ready_a["ragflowDocumentId"]]
    )
    assert authorized_ragflow_doc_ids_sent == (
        expected_authorized_ragflow_doc_ids
    ), authorized_ragflow_doc_ids_sent
    missing_authorized_ragflow_doc_ids = sorted(
        set(expected_authorized_ragflow_doc_ids)
        - set(authorized_ragflow_doc_ids_sent)
    )
    assert missing_authorized_ragflow_doc_ids == [], (
        missing_authorized_ragflow_doc_ids
    )
    for c in citations:
        assert c["documentId"] and c["title"]
        assert c["versionId"] == SOURCE_VERSION
        assert c["assetId"] == "FA-Doc1"
        assert c["pageNo"] is not None or c["bbox"] is not None

    client = RAGFlowQueryClient(api_key=key)
    chats_a = asyncio.run(
        client.list_chats(name=f"enterprise-formal-{TENANT}")
    )
    assert chats_a, "formal chat not found for tenant"
    chat_a_id = chats_a[0]["id"]

    second = gateway_post(
        f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
        headers=user_a_headers,
        json_body={"question": "What fixed asset number is listed in this document?"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "completed", second.text

    set_chat_top_n(chat_a_id, 0, key)
    try:
        no_evidence = gateway_post(
            f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
            headers=user_a_headers,
            json_body={
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
    assert no_evidence_body["status"] == "no_reliable_evidence", (
        no_evidence.text
    )
    assert no_evidence_body["answer"]
    assert no_evidence_body["citations"] == []

    no_evidence_history = gateway_get(
        f"/enterprise/api/v1/conversations/{conversation_id}",
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
        f"/enterprise/api/v1/conversations/{conversation_id}",
        user_a_headers,
    )
    assert history.status_code == 200, history.text
    messages = history.json()["messages"]
    assert len(messages) >= 6, messages

    cross_user_conversation = gateway_get(
        f"/enterprise/api/v1/conversations/{conversation_id}",
        user_b_headers,
    )
    assert cross_user_conversation.status_code == 404, (
        cross_user_conversation.text
    )

    cross_user_citation = gateway_get(
        f"/enterprise/api/v1/citations/{citations[0]['citationId']}",
        user_b_headers,
    )
    assert cross_user_citation.status_code == 404, cross_user_citation.text

    citation_detail = gateway_get(
        f"/enterprise/api/v1/citations/{citations[0]['citationId']}",
        user_a_headers,
    )
    assert citation_detail.status_code == 200, citation_detail.text
    assert citation_detail.json()["versionId"] == SOURCE_VERSION
    assert citation_detail.json()["assetId"] == "FA-Doc1"

    trace_start_unauthorized = len(trace_entries(QUERY_TRACE))
    unauthorized_document = gateway_post(
        f"/enterprise/api/v1/conversations/{conversation_id}/messages:stream",
        headers=user_a_headers,
        json_body={"question": "What equipment id is listed in Doc2?"},
    )
    assert unauthorized_document.status_code == 200, unauthorized_document.text
    unauthorized_citations = unauthorized_document.json()["citations"]
    unauthorized_document_leak_count = sum(
        1
        for c in unauthorized_citations
        if c["documentId"] == doc_b_id
    )
    assert unauthorized_document_leak_count == 0, unauthorized_citations
    unauthorized_sent = trace_doc_ids(QUERY_TRACE, trace_start_unauthorized)
    unauthorized_ragflow_doc_ids_sent = sorted(
        set(unauthorized_sent) - set(expected_authorized_ragflow_doc_ids)
    )
    assert unauthorized_ragflow_doc_ids_sent == [], unauthorized_sent

    denied_capability = gateway_post(
        "/enterprise/api/v1/conversations",
        headers=user_c_headers,
        json_body={},
    )
    assert denied_capability.status_code == 403, denied_capability.text
    assert denied_capability.json()["code"] == "ACL_DENIED"

    created_b = gateway_post(
        "/enterprise/api/v1/conversations",
        headers=user_b_headers,
        json_body={},
    )
    assert created_b.status_code == 201, created_b.text
    conversation_b_id = created_b.json()["conversationId"]
    trace_start_b = len(trace_entries(QUERY_TRACE))
    allowed_b = gateway_post(
        f"/enterprise/api/v1/conversations/{conversation_b_id}/messages:stream",
        headers=user_b_headers,
        json_body={"question": "What equipment id is listed in this document?"},
    )
    assert allowed_b.status_code == 200, allowed_b.text
    b_citations = allowed_b.json()["citations"]
    assert b_citations, "user B formal ask returned no citations"
    assert all(
        c["documentId"] == doc_b_id for c in b_citations
    ), b_citations
    assert all(
        c["versionId"] == SOURCE_VERSION and c["assetId"] == "FA-Doc2"
        for c in b_citations
    ), b_citations
    user_b_ragflow_doc_ids_sent = trace_doc_ids(QUERY_TRACE, trace_start_b)
    assert user_b_ragflow_doc_ids_sent == [ready_b["ragflowDocumentId"]], (
        user_b_ragflow_doc_ids_sent
    )

    transport_evidence = verify_transport_failure(user_a_headers)
    stream_transport_evidence = verify_stream_gateway_failure(
        user_a_headers,
        ready_a["ragflowDatasetId"],
        expected_authorized_ragflow_doc_ids,
        key,
    )

    chats = asyncio.run(
        client.list_chats(name=f"enterprise-formal-{TENANT}")
    )
    assert chats, "formal chat not found for tenant"
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
        "worktreeDirty": worktree_dirty(),
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
        "notReadyRequestCode": "no_reliable_evidence",
        "conversationCreateStatus": created_a.status_code,
        "conversationId": conversation_id,
        "askStreamStatus": first.status_code,
        "streamEventCount": len(first_events),
        "answerContentPresent": bool(stream_answer_text),
        "answerStatus": first_body["status"],
        "askAStatus": first.status_code,
        "askBStatus": allowed_b.status_code,
        "answerBusinessStatus": "completed",
        "noEvidenceRequestStatus": no_evidence.status_code,
        "noEvidenceCode": "NO_RELIABLE_EVIDENCE",
        "noEvidenceConversationId": conversation_id,
        "noEvidenceDocumentId": doc_a_id,
        "noEvidenceTrigger": "chat_top_n=0",
        "noEvidenceHistoryStatus": no_evidence_assistant["status"],
        "noEvidenceHistoryCitationCount": len(
            no_evidence_assistant["citations"]
        ),
        "noEvidenceHistoryContentPresent": bool(
            no_evidence_assistant["content"].strip()
        ),
        "userAAnswerPresent": bool(stream_answer_text),
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
        "conversationMessageCount": len(messages),
        "historyStatus": history.status_code,
        "historyMessageCount": len(messages),
        "crossUserConversationStatus": cross_user_conversation.status_code,
        "crossUserCitationStatus": cross_user_citation.status_code,
        "unauthorizedDocumentLeakCount": unauthorized_document_leak_count,
        "readOnlyCapabilityDeniedStatus": denied_capability.status_code,
        "userBCitationsDocumentIds": sorted(
            {c["documentId"] for c in b_citations}
        ),
        "expectedAuthorizedRagflowDocIds": (
            expected_authorized_ragflow_doc_ids
        ),
        "authorizedRagflowDocIdsSent": authorized_ragflow_doc_ids_sent,
        "unauthorizedRagflowDocIdsSent": (
            unauthorized_ragflow_doc_ids_sent
        ),
        "missingAuthorizedRagflowDocIds": (
            missing_authorized_ragflow_doc_ids
        ),
        "userBRagflowDocIdsSent": user_b_ragflow_doc_ids_sent,
        "ragflowDocIdsScopeLeakFree": (
            unauthorized_ragflow_doc_ids_sent == []
            and missing_authorized_ragflow_doc_ids == []
        ),
        "transportFailureStatus": transport_evidence[
            "transportFailureStatus"
        ],
        "transportFailureStage": transport_evidence[
            "transportFailureStage"
        ],
        "transportFailureHistoryStatus": transport_evidence[
            "transportFailureHistoryStatus"
        ],
        "streamTransportFailureVerified": stream_transport_evidence[
            "streamTransportFailureVerified"
        ],
        "streamTransportExceptionMapped": stream_transport_evidence[
            "streamTransportExceptionMapped"
        ],
        "streamTransportNameErrorObserved": stream_transport_evidence[
            "streamTransportNameErrorObserved"
        ],
        "streamTransportSensitiveDataLeaked": stream_transport_evidence[
            "streamTransportSensitiveDataLeaked"
        ],
        "streamTransportGatewayRunFailed": stream_transport_evidence[
            "streamTransportGatewayRunFailed"
        ],
        "streamTransportGatewayAnswerCompleted": (
            stream_transport_evidence[
                "streamTransportGatewayAnswerCompleted"
            ]
        ),
        "streamTransportGatewayHistoryStatus": (
            stream_transport_evidence[
                "streamTransportGatewayHistoryStatus"
            ]
        ),
        "streamTransportDocIdsSent": stream_transport_evidence[
            "streamTransportDocIdsSent"
        ],
        "s3Bucket": bucket,
        "passed": True,
    }
    REPORT_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
