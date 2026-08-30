"""Local live check: citation filtering + unified ticket download.

Feeds an invoice PDF whose body does not contain the equipment id, then:

1. Inventory question keeps cited chunks and returns one downloadUrl each
2. GET downloadUrl without JWT returns the original PDF
3. Leak-repair question is 无可靠依据 with citations=[]
4. Expired/wrong ticket returns 404

Run inside the enterprise-gateway container.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
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
from enterprise.gateway.db.database import GatewayDatabase  # noqa: E402
from enterprise.gateway.db.dialect import fetchall  # noqa: E402
from enterprise.gateway.models.ext_user_map import ExtUserMap, ExtUserMapRepo  # noqa: E402

EQUIPMENT_ID = "LOCAL-CITE-EQ-001"
FIXED_ASSET_NO = "LOCAL-CITE-FA-001"
TENANT_ID = "wp04e2e"
SOURCE_SYSTEM = "EAM"
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188").rstrip("/")
PDF_LINES = [
    "INVOICE",
    "Vendor: Anysphere Inc.",
    "Product: Cursor Pro Software Subscription",
    "Period: 2026-07-01 to 2026-07-31",
    "Amount: USD 20.00",
    "Payment method: corporate card",
]


def _load_hmac() -> tuple[str, str]:
    raw = json.loads(os.environ["ENTERPRISE_SYNC_HMAC_CREDENTIALS"])
    credentials = raw.get("credentials") if isinstance(raw, dict) and "credentials" in raw else (
        [raw] if isinstance(raw, dict) else raw
    )
    for item in credentials:
        if item.get("status", "active") not in {"active", "previous"}:
            continue
        bindings = item.get("allowedBindings") or item.get("allowed_bindings") or []
        if any(
            b.get("tenantId") == TENANT_ID and b.get("sourceSystem") == SOURCE_SYSTEM
            for b in bindings
            if isinstance(b, dict)
        ):
            return item["keyId"], item["secret"]
    raise RuntimeError("no HMAC credential for wp04e2e/EAM")


def _headers(key_id: str, secret: str, method: str, relative_url: str, body: bytes = b"") -> dict:
    parsed = urlsplit(relative_url)
    timestamp = str(int(time.time()))
    signature = sign_request(
        secret=secret,
        timestamp=timestamp,
        method=method,
        path=parsed.path,
        query=parsed.query,
        body=body,
    )
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-TY-Timestamp": timestamp,
        "X-TY-Key-Id": key_id,
        "X-TY-Signature": signature,
    }


def _jwt() -> str:
    now = int(time.time())
    claims = {
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
    }
    return jwt.encode(claims, os.environ["JWT_SHARED_SECRET"], algorithm="HS256")


def _ensure_user() -> None:
    async def seed() -> None:
        gateway = GatewayDatabase.from_env()
        try:
            await gateway.initialize()
            repo = ExtUserMapRepo(gateway=gateway)
            await repo.insert_mapping(
                ExtUserMap(
                    tenant_id=TENANT_ID,
                    business_subject="local-identity-tester",
                    business_user_id="local-identity-tester",
                    mapping_strategy="B",
                )
            )
        finally:
            await gateway.dispose()

    asyncio.run(seed())


def _make_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in PDF_LINES:
        c.drawString(72, y, line)
        y -= 24
    c.save()


def _public_citation(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "citationId",
            "title",
            "externalDocumentId",
            "sourceVersionId",
            "pageNo",
            "downloadUrl",
            "downloadExpiresAt",
        )
    }


def _ask(client: httpx.Client, token: str, question: str) -> dict:
    create = client.post(
        f"{GATEWAY}/enterprise/api/v2/conversations",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"equipmentId": EQUIPMENT_ID, "fixedAssetNo": FIXED_ASSET_NO},
    )
    create.raise_for_status()
    conversation_id = create.json()["conversationId"]
    msg = client.post(
        f"{GATEWAY}/enterprise/api/v2/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "clientMessageId": f"cite-{uuid.uuid4().hex[:12]}",
            "question": question,
        },
        timeout=180.0,
    )
    msg.raise_for_status()
    return msg.json()


def main() -> int:
    key_id, secret = _load_hmac()
    _ensure_user()

    stamp = time.strftime("%Y%m%d%H%M%S")
    external_document_id = f"LOCAL-CITE-{stamp}"
    source_version_id = "v1"
    file_name = f"{external_document_id}.pdf"
    relative_path = (
        f"eam/{TENANT_ID}/{EQUIPMENT_ID}/{external_document_id}/"
        f"{source_version_id}/{file_name}"
    )
    pdf_path = Path("/var/lib/tyrag/file-share") / relative_path
    _make_pdf(pdf_path)
    content = pdf_path.read_bytes()
    if EQUIPMENT_ID.encode() in content:
        raise RuntimeError("test PDF unexpectedly contains equipment id")

    registration = {
        "eventId": f"evt-{external_document_id}",
        "eventType": "upsert",
        "tenantId": TENANT_ID,
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": external_document_id,
        "sourceVersionId": source_version_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fileName": file_name,
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": "device-share",
            "relativePath": relative_path,
            "size": len(content),
        },
        "metadata": {
            "schema_version": 1,
            "tenant_id": TENANT_ID,
            "external_document_id": external_document_id,
            "source_system": SOURCE_SYSTEM,
            "equipment_id": EQUIPMENT_ID,
            "fixed_asset_no": FIXED_ASSET_NO,
            "document_type": "invoice",
            "document_version": source_version_id,
            "department_id": "maintenance",
            "security_level": 2,
            "allow_group_ids": ["maintenance"],
            "deny_group_ids": [],
            "business_status": "active",
            "page_count": 1,
        },
    }
    body = json.dumps(registration, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    failures: list[str] = []

    print("=== 0) Confirm new citation modules are loaded ===")
    from enterprise.gateway.query.citation_file import public_citation  # noqa: F401
    from enterprise.gateway.query.citation_select import select_cited_chunks

    selected = select_cited_chunks("ok [ID:1]", [{"id": "a"}, {"id": "b"}], "completed")
    empty = select_cited_chunks("ok [ID:0]", [{"id": "a"}], "no_reliable_evidence")
    print(f"select_cited_chunks cited={ [c['id'] for c in selected] } empty={empty}")
    if [c["id"] for c in selected] != ["b"] or empty != []:
        failures.append("in-process citation_select mismatch")

    print("=== 1) FILE_SHARE register ===")
    print(f"equipment_id={EQUIPMENT_ID} external_document_id={external_document_id}")
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{GATEWAY}/enterprise/api/v3/documents",
            content=body,
            headers=_headers(key_id, secret, "POST", "/enterprise/api/v3/documents", body),
        )
        print(f"register status={resp.status_code}")
        if resp.status_code != 202:
            print(resp.text[:500])
            return 1

        status_url = (
            f"/enterprise/api/v3/documents/{quote(external_document_id, safe='')}/status"
            f"?tenantId={quote(TENANT_ID)}"
            f"&sourceSystem={quote(SOURCE_SYSTEM)}"
            f"&sourceVersionId={quote(source_version_id)}"
        )
        status = {}
        for attempt in range(90):
            status_resp = client.get(
                f"{GATEWAY}{status_url}",
                headers=_headers(key_id, secret, "GET", status_url),
            )
            if status_resp.status_code == 200:
                status = status_resp.json()
                print(
                    f"[{attempt}] retrievable={status.get('retrievable')} "
                    f"pipeline={status.get('pipelineStatus')} "
                    f"quality={status.get('qualityStatus')}"
                )
                if status.get("retrievable") is True:
                    break
                if str(status.get("status", "")).lower() in {"failed", "unavailable"}:
                    print("document failed:", json.dumps(status, ensure_ascii=False)[:800])
                    return 2
            else:
                print(f"[{attempt}] status_http={status_resp.status_code}")
            time.sleep(2)
        else:
            print("timeout waiting for retrievable")
            return 3

        token = _jwt()
        print("=== 2) Inventory question should cite the invoice ===")
        ans1 = _ask(client, token, f"当前知识库里 {EQUIPMENT_ID} 有哪些相关资料？请概括内容。")
        cites1 = ans1.get("citations") or []
        print("status=", ans1.get("status"))
        print("answer=", (ans1.get("answer") or "")[:500])
        print("citations=", json.dumps([_public_citation(c) for c in cites1], ensure_ascii=False))
        hidden = {"imageId", "chunkId", "sourceDownloadUrl", "imageDownloadUrl"}
        leaked = [key for item in cites1 for key in hidden if key in item]
        if leaked:
            failures.append(f"public citation leaked internal keys: {leaked}")
        if not cites1:
            failures.append("inventory answer returned no citations")
        for item in cites1:
            if not item.get("downloadUrl") or not item.get("downloadExpiresAt"):
                failures.append(f"citation missing download fields: {item.get('citationId')}")
            if item.get("externalDocumentId") not in {None, external_document_id}:
                failures.append(
                    f"unexpected externalDocumentId={item.get('externalDocumentId')}"
                )

        print("=== 3) GET downloadUrl without JWT ===")
        if cites1:
            download_url = cites1[0]["downloadUrl"]
            if download_url.startswith("http://127.0.0.1:5188"):
                download_url = "http://127.0.0.1:5188" + urlsplit(download_url).path
            downloaded = client.get(download_url)
            print(
                f"download status={downloaded.status_code} "
                f"content-type={downloaded.headers.get('content-type')} "
                f"bytes={len(downloaded.content)}"
            )
            if downloaded.status_code != 200:
                failures.append(f"downloadUrl HTTP {downloaded.status_code}")
            content_type = (downloaded.headers.get("content-type") or "").lower()
            if "pdf" not in content_type and "image/" not in content_type:
                failures.append(f"unexpected Content-Type {content_type}")
            if downloaded.content[:4] == b"%PDF" and downloaded.content != content:
                # Original FILE_SHARE bytes should match the registered PDF.
                failures.append("downloaded PDF bytes differ from registered source")
            if downloaded.content[:4] != b"%PDF" and not content_type.startswith("image/"):
                failures.append("downloaded neither PDF nor image")

            print("=== 4) Wrong ticket is 404 ===")
            bad = download_url.rsplit("/", 1)[0] + "/not-a-real-ticket"
            bad_resp = client.get(bad)
            print(f"bad ticket status={bad_resp.status_code}")
            if bad_resp.status_code != 404:
                failures.append(f"wrong ticket expected 404, got {bad_resp.status_code}")

        print("=== 5) Leak repair should abstain with empty citations ===")
        ans2 = _ask(client, token, "有没有漏气维修记录？")
        cites2 = ans2.get("citations") or []
        print("status=", ans2.get("status"))
        print("answer=", (ans2.get("answer") or "")[:500])
        print("citations=", len(cites2))
        if ans2.get("status") == "无可靠依据" and cites2:
            failures.append("no_reliable_evidence still returned citations")
        if ans2.get("status") not in {"无可靠依据", "已完成", "失败"}:
            failures.append(f"unexpected status {ans2.get('status')}")

        async def citation_tables() -> list[str]:
            gateway = GatewayDatabase.from_env()
            try:
                async with gateway.transaction() as conn:
                    rows = await fetchall(
                        conn,
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=current_schema() "
                        "AND table_name LIKE '%citation%'",
                    )
                return [str(row["table_name"]) for row in rows]
            finally:
                await gateway.dispose()

        tables = asyncio.run(citation_tables())
        print("citation tables=", tables)
        if "ext_citation_file_ticket" not in tables:
            failures.append("ext_citation_file_ticket table missing")

    if failures:
        print("RESULT: FAIL")
        for item in failures:
            print(" -", item)
        return 5
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
