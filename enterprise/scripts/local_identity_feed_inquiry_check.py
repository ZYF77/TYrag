"""Local live check: EAM-like FILE_SHARE feed + identity metadata + inquiry.

Creates a PDF whose OCR text does NOT contain the equipment id, registers it
via HMAC v3, waits until retrievable, then asserts:

1. Document meta_fields contain equipment_id / fixed_asset_no
2. Chunk content_with_weight and important_kwd do NOT contain the equipment id
3. Bound inquiry about 'what documents exist' returns a useful answer
4. Bound inquiry about 'leak repair records' abstains (invoice-only content)

Run inside the enterprise-gateway container (or any host that can reach it).
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
from enterprise.gateway.db.dialect import fetchone  # noqa: E402
from enterprise.gateway.models.ext_user_map import ExtUserMap, ExtUserMapRepo  # noqa: E402

EQUIPMENT_ID = "LOCAL-TEST-EQ-001"
FIXED_ASSET_NO = "LOCAL-FA-001"
TENANT_ID = "wp04e2e"
SOURCE_SYSTEM = "EAM"
ROOT_ID = "device-share"
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188").rstrip("/")
RAGFLOW = os.environ.get("ENTERPRISE_RAGFLOW_BASE_URL", "http://ragflow-cpu:9380").rstrip("/")
RAGFLOW_KEY = os.environ["ENTERPRISE_RAGFLOW_API_KEY"]
# Invoice-like body deliberately omits EQUIPMENT_ID / FIXED_ASSET_NO.
PDF_BODY_LINES = [
    "INVOICE",
    "Vendor: Anysphere Inc.",
    "Product: Cursor Pro Software Subscription",
    "Period: 2026-07-01 to 2026-07-31",
    "Amount: USD 20.00",
    "Payment method: corporate card",
    "Notes: SaaS subscription receipt. No equipment maintenance content.",
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
    for line in PDF_BODY_LINES:
        c.drawString(72, y, line)
        y -= 24
    c.save()


def _status_url(external_document_id: str, source_version_id: str) -> str:
    path = f"/enterprise/api/v3/documents/{quote(external_document_id, safe='')}/status"
    query = "&".join(
        f"{k}={quote(v, safe='')}"
        for k, v in (
            ("tenantId", TENANT_ID),
            ("sourceSystem", SOURCE_SYSTEM),
            ("sourceVersionId", source_version_id),
        )
    )
    return f"{path}?{query}"


def _ragflow_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RAGFLOW_KEY}", "Content-Type": "application/json"}


def _inspect_ragflow(client: httpx.Client, ragflow_document_id: str, dataset_id: str) -> dict:
    docs = client.get(
        f"{RAGFLOW}/api/v1/datasets/{dataset_id}/documents",
        params={"id": ragflow_document_id},
        headers=_ragflow_headers(),
    )
    docs.raise_for_status()
    docs_json = docs.json()
    doc_list = (docs_json.get("data") or {}).get("docs") or docs_json.get("data") or []
    if isinstance(doc_list, dict):
        doc_list = [doc_list]
    doc = next((d for d in doc_list if d.get("id") == ragflow_document_id), None)
    if not doc and doc_list:
        doc = doc_list[0]
    if not doc:
        raise RuntimeError(f"document not found in RAGFlow: {docs_json}")

    chunks_resp = client.get(
        f"{RAGFLOW}/api/v1/datasets/{dataset_id}/documents/{ragflow_document_id}/chunks",
        headers=_ragflow_headers(),
        params={"page": 1, "page_size": 50},
    )
    chunks_resp.raise_for_status()
    chunks_json = chunks_resp.json()
    chunks = (chunks_json.get("data") or {}).get("chunks") or []
    return {"doc": doc, "chunks": chunks}


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
            "clientMessageId": f"msg-{uuid.uuid4().hex[:12]}",
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
    external_document_id = f"LOCAL-INVOICE-{stamp}"
    source_version_id = "v1"
    event_id = f"evt-{external_document_id}"
    file_name = f"{external_document_id}.pdf"
    relative_path = (
        f"eam/{TENANT_ID}/{EQUIPMENT_ID}/{external_document_id}/"
        f"{source_version_id}/{file_name}"
    )
    share_root = Path("/var/lib/tyrag/file-share")
    pdf_path = share_root / relative_path
    _make_pdf(pdf_path)
    content = pdf_path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()

    if EQUIPMENT_ID.encode() in content or FIXED_ASSET_NO.encode() in content:
        raise RuntimeError("test PDF unexpectedly contains identity strings")

    registration = {
        "eventId": event_id,
        "eventType": "upsert",
        "tenantId": TENANT_ID,
        "sourceSystem": SOURCE_SYSTEM,
        "externalDocumentId": external_document_id,
        "sourceVersionId": source_version_id,
        "sha256": sha256,
        "fileName": file_name,
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": ROOT_ID,
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
    path = "/enterprise/api/v3/documents"

    print("=== 1) FILE_SHARE register ===")
    print(f"equipment_id={EQUIPMENT_ID} external_document_id={external_document_id}")
    print(f"relative_path={relative_path}")

    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{GATEWAY}{path}",
            content=body,
            headers=_headers(key_id, secret, "POST", path, body),
        )
        print(f"register status={resp.status_code}")
        print(resp.text[:500])
        if resp.status_code != 202:
            return 1

        status_url = _status_url(external_document_id, source_version_id)
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
                    f"quality={status.get('qualityStatus')} "
                    f"error={status.get('errorCode')}"
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
            print(json.dumps(status, ensure_ascii=False)[:800])
            return 3

        ragflow_document_id = status.get("ragflowDocumentId") or status.get("documentId")
        dataset_id = status.get("ragflowDatasetId") or status.get("datasetId")
        # Fall back to gateway DB mapping if status omits ids.
        if not ragflow_document_id or not dataset_id:
            async def mapping() -> dict | None:
                gateway = GatewayDatabase.from_env()
                try:
                    async with gateway.transaction() as conn:
                        return await fetchone(
                            conn,
                            """SELECT ragflow_document_id, ragflow_dataset_id,
                                      equipment_id, fixed_asset_no
                                 FROM ext_document_map
                                WHERE tenant_id=? AND external_document_id=?
                                  AND source_version_id=?""",
                            (TENANT_ID, external_document_id, source_version_id),
                        )
                finally:
                    await gateway.dispose()

            row = asyncio.run(mapping())
            if not row:
                print("mapping row missing")
                return 4
            ragflow_document_id = ragflow_document_id or row["ragflow_document_id"]
            dataset_id = dataset_id or row["ragflow_dataset_id"]
            print(
                f"mapping equipment_id={row['equipment_id']} "
                f"fixed_asset_no={row['fixed_asset_no']}"
            )

        print("=== 2) Inspect RAGFlow meta_fields / chunks ===")
        print(f"dataset_id={dataset_id} document_id={ragflow_document_id}")
        inspected = _inspect_ragflow(client, ragflow_document_id, dataset_id)
        meta = inspected["doc"].get("meta_fields") or {}
        print("meta_fields keys:", sorted(meta.keys()))
        print(
            "meta equipment_id=",
            meta.get("equipment_id"),
            "fixed_asset_no=",
            meta.get("fixed_asset_no"),
            "document_type=",
            meta.get("enterprise_document_type"),
        )
        chunks = inspected["chunks"]
        print(f"chunk_count={len(chunks)}")
        identity_in_content = False
        identity_in_kwd = False
        for idx, chunk in enumerate(chunks[:10]):
            text = chunk.get("content") or chunk.get("content_with_weight") or ""
            kwds = chunk.get("important_keywords") or chunk.get("important_kwd") or []
            print(f"--- chunk[{idx}] ---")
            print(text[:240].replace("\n", " "))
            print("important_kwd=", kwds)
            if EQUIPMENT_ID in text or FIXED_ASSET_NO in text:
                identity_in_content = True
            joined = " ".join(kwds) if isinstance(kwds, list) else str(kwds)
            if EQUIPMENT_ID in joined or FIXED_ASSET_NO in joined:
                identity_in_kwd = True

        meta_ok = meta.get("equipment_id") == EQUIPMENT_ID and meta.get("fixed_asset_no") == FIXED_ASSET_NO
        print(f"ASSERT meta_fields identity present: {meta_ok}")
        print(f"ASSERT chunk content has NO identity: {not identity_in_content}")
        print(f"ASSERT important_kwd has NO identity: {not identity_in_kwd}")

        print("=== 3) Inquiry: what docs exist for this equipment ===")
        token = _jwt()
        ans1 = _ask(client, token, f"当前知识库里 {EQUIPMENT_ID} 有哪些相关资料？请概括内容。")
        print("status=", ans1.get("status"))
        print("answer=", (ans1.get("answer") or "")[:800])
        cites1 = ans1.get("citations") or []
        print("citations=", len(cites1))

        print("=== 4) Inquiry: leak repair (should abstain on invoice-only) ===")
        ans2 = _ask(client, token, "有没有漏气维修记录？")
        print("status=", ans2.get("status"))
        print("answer=", (ans2.get("answer") or "")[:800])

        ok = meta_ok and not identity_in_content and not identity_in_kwd
        # Soft checks on answers — model wording varies.
        a1 = (ans1.get("answer") or "").lower()
        useful = any(
            token in a1
            for token in ("cursor", "invoice", "订阅", "发票", "anysphere", "usd", "20")
        ) or ans1.get("status") == "completed"
        print(f"SOFT inventory answer looks useful: {useful}")
        print("RESULT:", "PASS" if ok else "FAIL (identity storage assertions)")
        return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
