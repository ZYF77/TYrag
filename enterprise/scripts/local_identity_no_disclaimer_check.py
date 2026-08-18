"""Retest: invoice PDF without 'No equipment maintenance content' disclaimer."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import jwt
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.gateway.auth.service_auth import sign_request  # noqa: E402
from enterprise.gateway.models.ext_user_map import ExtUserMap, ExtUserMapRepo  # noqa: E402
from enterprise.gateway.sync.models import init_db  # noqa: E402

EQUIPMENT_ID = "LOCAL-TEST-EQ-002"
FIXED_ASSET_NO = "LOCAL-FA-002"
TENANT_ID = "wp04e2e"
SOURCE_SYSTEM = "EAM"
GATEWAY = "http://127.0.0.1:5188"
RAGFLOW = os.environ["ENTERPRISE_RAGFLOW_BASE_URL"].rstrip("/")
KEY = os.environ["ENTERPRISE_RAGFLOW_API_KEY"]
LINES = [
    "INVOICE",
    "Vendor: Anysphere Inc.",
    "Product: Cursor Pro Software Subscription",
    "Period: 2026-07-01 to 2026-07-31",
    "Amount: USD 20.00",
    "Payment method: corporate card",
]


def _hmac() -> tuple[str, str]:
    raw = json.loads(os.environ["ENTERPRISE_SYNC_HMAC_CREDENTIALS"])
    creds = raw.get("credentials", [raw]) if isinstance(raw, dict) else raw
    for item in creds:
        for binding in item.get("allowedBindings") or []:
            if (
                binding.get("tenantId") == TENANT_ID
                and binding.get("sourceSystem") == SOURCE_SYSTEM
            ):
                return item["keyId"], item["secret"]
    raise RuntimeError("hmac missing")


def _headers(key_id: str, secret: str, method: str, url: str, body: bytes = b"") -> dict:
    parsed = urlsplit(url)
    ts = str(int(time.time()))
    sig = sign_request(
        secret=secret,
        timestamp=ts,
        method=method,
        path=parsed.path,
        query=parsed.query,
        body=body,
    )
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-TY-Timestamp": ts,
        "X-TY-Key-Id": key_id,
        "X-TY-Signature": sig,
    }


def main() -> int:
    key_id, secret = _hmac()

    async def seed() -> None:
        db_path = os.environ["ENTERPRISE_SYNC_DB_PATH"]
        db = await init_db(db_path)
        await db.close()
        repo = ExtUserMapRepo(db_path=db_path)
        try:
            await repo.ensure_table()
            await repo.insert_mapping(
                ExtUserMap(
                    tenant_id=TENANT_ID,
                    business_subject="local-identity-tester",
                    business_user_id="local-identity-tester",
                    mapping_strategy="B",
                )
            )
        finally:
            await repo.close()

    import asyncio

    asyncio.run(seed())

    stamp = time.strftime("%Y%m%d%H%M%S")
    ext = f"LOCAL-INVOICE-NODISCLAIMER-{stamp}"
    ver = "v1"
    fname = f"{ext}.pdf"
    rel = f"eam/{TENANT_ID}/{EQUIPMENT_ID}/{ext}/{ver}/{fname}"
    path = Path("/var/lib/tyrag/file-share") / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in LINES:
        c.drawString(72, y, line)
        y -= 24
    c.save()
    content = path.read_bytes()
    assert b"No equipment maintenance" not in content
    assert EQUIPMENT_ID.encode() not in content
    sha = hashlib.sha256(content).hexdigest()

    reg = {
        "eventId": f"evt-{ext}",
        "eventType": "upsert",
        "tenantId": TENANT_ID,
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": ext,
        "sourceVersionId": ver,
        "sha256": sha,
        "fileName": fname,
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "device-share",
            "relativePath": rel,
            "size": len(content),
        },
        "metadata": {
            "schema_version": 1,
            "tenant_id": TENANT_ID,
            "external_document_id": ext,
            "source_system": SOURCE_SYSTEM,
            "equipment_id": EQUIPMENT_ID,
            "fixed_asset_no": FIXED_ASSET_NO,
            "document_type": "invoice",
            "document_version": ver,
            "department_id": "maintenance",
            "security_level": 2,
            "allow_group_ids": ["maintenance"],
            "deny_group_ids": [],
            "business_status": "active",
            "page_count": 1,
        },
    }
    body = json.dumps(reg, ensure_ascii=False, separators=(",", ":")).encode()
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            f"{GATEWAY}/enterprise/api/v3/documents",
            content=body,
            headers=_headers(key_id, secret, "POST", "/enterprise/api/v3/documents", body),
        )
        print("register", r.status_code, r.text[:200])
        if r.status_code != 202:
            return 1
        status_path = (
            f"/enterprise/api/v3/documents/{quote(ext, safe='')}/status"
            f"?tenantId={quote(TENANT_ID)}"
            f"&sourceSystem={quote(SOURCE_SYSTEM)}"
            f"&sourceVersionId={quote(ver)}"
        )
        status = {}
        for i in range(90):
            sr = client.get(
                f"{GATEWAY}{status_path}",
                headers=_headers(key_id, secret, "GET", status_path),
            )
            if sr.status_code == 200:
                status = sr.json()
                print(
                    i,
                    status.get("retrievable"),
                    status.get("pipelineStatus"),
                    status.get("qualityStatus"),
                )
                if status.get("retrievable") is True:
                    break
            time.sleep(2)
        else:
            print("timeout", status)
            return 2

        db = sqlite3.connect(os.environ["ENTERPRISE_SYNC_DB_PATH"])
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT ragflow_document_id, ragflow_dataset_id FROM ext_document_map "
            "WHERE external_document_id=?",
            (ext,),
        ).fetchone()
        db.close()
        chunks = client.get(
            f"{RAGFLOW}/api/v1/datasets/{row['ragflow_dataset_id']}/documents/"
            f"{row['ragflow_document_id']}/chunks",
            headers={"Authorization": f"Bearer {KEY}"},
            params={"page": 1, "page_size": 10},
        ).json()
        text = ((chunks.get("data") or {}).get("chunks") or [{}])[0].get("content") or ""
        print("CHUNK:", text[:300])
        print("has_disclaimer_phrase:", "No equipment maintenance" in text)

        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "local-identity-tester",
                "tenant": TENANT_ID,
                "business_user_id": "local-identity-tester",
                "name": "local-identity-tester",
                "department": ["maintenance"],
                "roles": ["end_user"],
                "groups": ["maintenance"],
                "security_level": 2,
                "iat": now - 5,
                "exp": now + 1800,
                "iss": os.environ["JWT_ISSUER"],
                "aud": os.environ["JWT_AUDIENCE"],
            },
            os.environ["JWT_SHARED_SECRET"],
            algorithm="HS256",
        )
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        conv = client.post(
            f"{GATEWAY}/enterprise/api/v2/conversations",
            headers=h,
            json={"equipmentId": EQUIPMENT_ID, "fixedAssetNo": FIXED_ASSET_NO},
        ).json()
        ans = client.post(
            f"{GATEWAY}/enterprise/api/v2/conversations/{conv['conversationId']}/messages",
            headers=h,
            json={
                "clientMessageId": f"m-{uuid.uuid4().hex[:8]}",
                "question": "有没有漏气维修记录？",
            },
            timeout=180.0,
        ).json()
        print("status=", ans.get("status"))
        print("answer=", (ans.get("answer") or "")[:1000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
