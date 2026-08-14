"""Required local happy-path E2E for FILE_SHARE v3 and formal v2.

This script is intentionally live-only.  It signs v3 registration requests
with the configured HMAC credential, asserts the slim 3.1.0 accept receipt,
then polls the diagnostic GET status URL built from registration identity.
Production EAM integration consumes the outbound terminal callback instead of
polling.  The suite never falls back to v1, S3, demo, or mocked services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.gateway.auth.service_auth import sign_request  # noqa: E402
from enterprise.gateway.models.ext_user_map import (  # noqa: E402
    ExtUserMap,
    ExtUserMapRepo,
)
from enterprise.gateway.sync.models import init_db  # noqa: E402


class LiveEnvironmentError(RuntimeError):
    """The required live environment is not configured or reachable."""


class LiveAssertionError(RuntimeError):
    """The required live contract returned an unexpected result."""


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise LiveEnvironmentError("required live configuration is missing")
    return value or default


def _json_response(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise LiveAssertionError("live endpoint returned non-JSON") from exc
    if not isinstance(value, dict):
        raise LiveAssertionError("live endpoint returned an invalid JSON object")
    return value


def _load_hmac_credential(tenant_id: str, source_system: str) -> tuple[str, str]:
    raw = _env("ENTERPRISE_SYNC_HMAC_CREDENTIALS")
    try:
        value = json.loads(raw)
        credentials = (
            value.get("credentials") if "credentials" in value else [value]
        ) if isinstance(value, dict) else value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveEnvironmentError("HMAC credential configuration is invalid") from exc
    if not isinstance(credentials, list):
        raise LiveEnvironmentError("HMAC credential configuration is invalid")
    for item in credentials:
        if not isinstance(item, dict) or item.get("status", "active") not in {
            "active",
            "previous",
        }:
            continue
        bindings = item.get("allowedBindings", item.get("allowed_bindings", []))
        if not isinstance(bindings, list):
            continue
        if any(
            isinstance(binding, dict)
            and binding.get("tenantId", binding.get("tenant_id")) == tenant_id
            and binding.get("sourceSystem", binding.get("source_system"))
            == source_system
            for binding in bindings
        ):
            key_id = item.get("keyId", item.get("key_id"))
            secret = item.get("secret")
            if isinstance(key_id, str) and key_id and isinstance(secret, str) and secret:
                return key_id, secret
    raise LiveEnvironmentError("no HMAC credential is bound to the live scope")


def _file_path(root_id: str, relative_path: str) -> Path:
    local_root = os.environ.get("ENTERPRISE_FILE_SHARE_LOCAL_ROOT", "").strip()
    if not local_root:
        raw = _env("ENTERPRISE_FILE_SHARE_ROOTS")
        try:
            roots = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LiveEnvironmentError("FILE_SHARE root configuration is invalid") from exc
        local_root = roots.get(root_id, "") if isinstance(roots, dict) else ""
    if not isinstance(local_root, str) or not local_root:
        raise LiveEnvironmentError("FILE_SHARE root is not configured")
    path = (Path(local_root) / relative_path).resolve()
    if not path.is_file():
        raise LiveEnvironmentError("FILE_SHARE test document is unavailable")
    return path


def _stage_unique_source_copy(
    source_path: Path, relative_path: str
) -> tuple[Path, str]:
    unique_name = (
        f"{source_path.stem}-e2e-{uuid.uuid4().hex[:12]}{source_path.suffix}"
    )
    staged_path = source_path.with_name(unique_name)
    shutil.copyfile(source_path, staged_path)
    staged_relative_path = Path(relative_path).with_name(unique_name).as_posix()
    return staged_path, staged_relative_path


def _metadata(
    *,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    equipment_id: str,
    fixed_asset_no: str,
) -> dict:
    return {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "external_document_id": external_document_id,
        "source_system": source_system,
        "equipment_id": equipment_id,
        "fixed_asset_no": fixed_asset_no or None,
        "document_type": os.environ.get(
            "ENTERPRISE_E2E_DOCUMENT_TYPE", "PRODUCT_MANUAL"
        ),
        "document_version": source_version_id,
        "department_id": os.environ.get("ENTERPRISE_E2E_DEPARTMENT_ID", "maintenance"),
        "security_level": int(os.environ.get("ENTERPRISE_E2E_SECURITY_LEVEL", "2")),
        "business_status": "active",
        "allow_group_ids": [
            item.strip()
            for item in os.environ.get("ENTERPRISE_E2E_ALLOW_GROUPS", "maintenance").split(",")
            if item.strip()
        ],
        "deny_group_ids": [],
        "page_count": 1,
    }


def _service_headers(
    *,
    key_id: str,
    secret: str,
    method: str,
    relative_url: str,
    body: bytes = b"",
) -> dict[str, str]:
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


def _jwt_token(
    *,
    secret: str,
    issuer: str,
    audience: str,
    subject: str,
    tenant_id: str,
) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "tenant": tenant_id,
        "business_user_id": subject,
        "name": subject,
        "department": [os.environ.get("ENTERPRISE_E2E_DEPARTMENT_ID", "maintenance")],
        "roles": ["end_user"],
        "groups": [
            item.strip()
            for item in os.environ.get("ENTERPRISE_E2E_USER_GROUPS", "maintenance").split(",")
            if item.strip()
        ],
        "security_level": int(os.environ.get("ENTERPRISE_E2E_SECURITY_LEVEL", "2")),
        "iat": now - 5,
        "exp": now + 900,
        "iss": issuer,
        "aud": audience,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def build_diagnostic_status_url(
    *,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> str:
    """Build the ops/diagnostic GET status URL from registration identity.

    FILE_SHARE 3.1.0 registration no longer returns statusUrl. Live suites and
    console tooling may still poll GET status while EAM consumes callbacks.
    """
    path = (
        f"/enterprise/api/v3/documents/"
        f"{quote(external_document_id, safe='')}/status"
    )
    query = "&".join(
        f"{key}={quote(value, safe='')}"
        for key, value in (
            ("tenantId", tenant_id),
            ("sourceSystem", source_system),
            ("sourceVersionId", source_version_id),
        )
    )
    return f"{path}?{query}"


def validate_accept_receipt(
    payload: dict,
    *,
    external_document_id: str,
    source_version_id: str,
) -> None:
    required = {
        "operationId",
        "externalDocumentId",
        "sourceVersionId",
        "deduplicated",
        "updatedAt",
    }
    if not required <= set(payload):
        raise LiveAssertionError("202 response is missing accept receipt fields")
    if "statusUrl" in payload:
        raise LiveAssertionError("202 response must not include statusUrl")
    if payload.get("externalDocumentId") != external_document_id:
        raise LiveAssertionError("accept receipt externalDocumentId mismatch")
    if payload.get("sourceVersionId") != source_version_id:
        raise LiveAssertionError("accept receipt sourceVersionId mismatch")


def matching_ingested_citations(
    citations: object,
    *,
    external_document_id: str,
    source_version_id: str,
) -> list[dict]:
    if not isinstance(citations, list):
        return []
    return [
        citation
        for citation in citations
        if isinstance(citation, dict)
        and citation.get("externalDocumentId") == external_document_id
        and citation.get("sourceVersionId") == source_version_id
        and bool(citation.get("citationId"))
    ]


def _ensure_user_mapping(db_path: str, tenant_id: str, subject: str) -> None:
    async def seed() -> None:
        db = await init_db(db_path)
        await db.close()
        repo = ExtUserMapRepo(db_path=db_path)
        try:
            await repo.ensure_table()
            await repo.insert_mapping(
                ExtUserMap(
                    tenant_id=tenant_id,
                    business_subject=subject,
                    business_user_id=subject,
                    mapping_strategy="B",
                )
            )
        finally:
            await repo.close()

    import asyncio

    asyncio.run(seed())


def run_live() -> dict[str, bool]:
    gateway = _env("GATEWAY_URL").rstrip("/")
    tenant_id = _env("ENTERPRISE_E2E_TENANT_ID", default="tyrag-integration")
    source_system = _env("ENTERPRISE_E2E_SOURCE_SYSTEM", default="EAM")
    root_id = _env("ENTERPRISE_E2E_STORAGE_ROOT_ID", default="device-share")
    relative_path = _env(
        "ENTERPRISE_E2E_FILE_RELATIVE_PATH", required=False, default="Doc1.pdf"
    )
    source_version_id = _env(
        "ENTERPRISE_E2E_SOURCE_VERSION_ID", required=False, default="v3-1"
    )
    external_document_id = _env(
        "ENTERPRISE_E2E_EXTERNAL_DOCUMENT_ID",
        required=False,
        default=f"TYRAG-E2E-{time.time_ns()}",
    )
    event_id = _env(
        "ENTERPRISE_E2E_EVENT_ID",
        required=False,
        default=f"evt-{external_document_id}",
    )
    equipment_id = _env("ENTERPRISE_E2E_EQUIPMENT_ID")
    fixed_asset_no = _env("ENTERPRISE_E2E_FIXED_ASSET_NO", required=False)
    subject = _env("ENTERPRISE_E2E_USER_SUBJECT", required=False, default="tyrag-e2e-user")
    db_path = _env("ENTERPRISE_DB_PATH")
    hmac_key_id, hmac_secret = _load_hmac_credential(tenant_id, source_system)
    jwt_secret = _env("JWT_SHARED_SECRET")
    issuer = _env("JWT_ISSUER")
    audience = _env("JWT_AUDIENCE")
    source_path, relative_path = _stage_unique_source_copy(
        _file_path(root_id, relative_path), relative_path
    )
    content = source_path.read_bytes()
    source_sha256 = hashlib.sha256(content).hexdigest()
    source_stat = source_path.stat()
    source_etag = f'"{source_stat.st_size:x}-{source_stat.st_mtime_ns:x}"'
    _ensure_user_mapping(db_path, tenant_id, subject)

    registration = {
        "eventId": event_id,
        "eventType": "upsert",
        "tenantId": tenant_id,
        "sourceSystem": source_system,
        "externalDocumentId": external_document_id,
        "sourceVersionId": source_version_id,
        "sha256": source_sha256,
        "fileName": source_path.name,
        "mediaType": "application/pdf",
        "source": {
            "kind": "FILE_SHARE",
            "storageRootId": root_id,
            "relativePath": relative_path,
            "size": len(content),
            "etag": source_etag,
        },
        "metadata": _metadata(
            tenant_id=tenant_id,
            source_system=source_system,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
        ),
    }
    registration_body = json.dumps(
        registration, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    registration_path = "/enterprise/api/v3/documents"
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{gateway}{registration_path}",
            content=registration_body,
            headers=_service_headers(
                key_id=hmac_key_id,
                secret=hmac_secret,
                method="POST",
                relative_url=registration_path,
                body=registration_body,
            ),
        )
        if response.status_code != 202:
            raise LiveAssertionError("FILE_SHARE registration did not return 202")
        receipt = _json_response(response)
        validate_accept_receipt(
            receipt,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
        )
        operation_id = receipt.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise LiveAssertionError("202 response did not contain operationId")
        status_url = build_diagnostic_status_url(
            tenant_id=tenant_id,
            source_system=source_system,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
        )

        retrievable = False
        for _ in range(int(os.environ.get("ENTERPRISE_E2E_STATUS_ATTEMPTS", "120"))):
            status_response = client.get(
                f"{gateway}{status_url}",
                headers=_service_headers(
                    key_id=hmac_key_id,
                    secret=hmac_secret,
                    method="GET",
                    relative_url=status_url,
                ),
            )
            if status_response.status_code == 200:
                status = _json_response(status_response)
                if status.get("retrievable") is True:
                    if (
                        str(status.get("pipelineStatus", "")).upper() not in {"DONE", "3"}
                        or status.get("parseCompleted") is not True
                        or status.get("indexCompleted") is not True
                        or status.get("qualityStatus") != "passed"
                        or status.get("errorCode") is not None
                    ):
                        raise LiveAssertionError(
                            "retrievable status is missing completed parse/index facts"
                        )
                    retrievable = True
                    break
                status_name = str(status.get("status", "")).lower()
                event_name = str(status.get("event", "")).lower()
                if (
                    status_name in {"failed", "unavailable"}
                    or "fail" in event_name
                    or status.get("error")
                ):
                    raise LiveAssertionError("FILE_SHARE document became unavailable")
            elif status_response.status_code not in {404, 409, 422}:
                raise LiveAssertionError("statusUrl returned an unexpected response")
            time.sleep(float(os.environ.get("ENTERPRISE_E2E_STATUS_INTERVAL", "2")))
        if not retrievable:
            raise LiveAssertionError("FILE_SHARE document did not become retrievable")

        token = _jwt_token(
            secret=jwt_secret,
            issuer=issuer,
            audience=audience,
            subject=subject,
            tenant_id=tenant_id,
        )
        user_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        conversation_response = client.post(
            f"{gateway}/enterprise/api/v2/conversations",
            headers=user_headers,
            json={
                "equipmentId": equipment_id,
                **({"fixedAssetNo": fixed_asset_no} if fixed_asset_no else {}),
            },
        )
        if conversation_response.status_code != 201:
            raise LiveAssertionError("formal v2 conversation was not created")
        conversation = _json_response(conversation_response)
        conversation_id = conversation.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise LiveAssertionError("formal v2 conversationId is missing")

        citations: list[dict] = []
        questions = tuple(
            f"{question}\nUse evidence from the file named {source_path.name}."
            for question in (
                _env(
                    "ENTERPRISE_E2E_QUESTION_ONE",
                    required=False,
                    default="Summarize the maintenance instructions in the newly ingested manual.",
                ),
                _env(
                    "ENTERPRISE_E2E_QUESTION_TWO",
                    required=False,
                    default="What safety checks are stated in the newly ingested manual?",
                ),
            )
        )
        for index, question in enumerate(questions, start=1):
            message_response = client.post(
                f"{gateway}/enterprise/api/v2/conversations/{quote(conversation_id, safe='-._~')}/messages",
                headers=user_headers,
                json={"clientMessageId": f"e2e-message-{index}", "question": question},
            )
            if message_response.status_code != 200:
                raise LiveAssertionError("formal v2 message was not completed")
            message = _json_response(message_response)
            if not message.get("answer") or message.get("status") != "completed":
                raise LiveAssertionError("formal v2 message has no completed answer")
            message_citations = message.get("citations")
            if not isinstance(message_citations, list) or not message_citations:
                raise LiveAssertionError("formal v2 message has no citation")
            matching_citations = matching_ingested_citations(
                message_citations,
                external_document_id=external_document_id,
                source_version_id=source_version_id,
            )
            if not matching_citations:
                raise LiveAssertionError("citation scope does not include ingested document")
            citations.extend(matching_citations)

        history_response = client.get(
            f"{gateway}/enterprise/api/v2/conversations/{quote(conversation_id, safe='-._~')}/messages",
            headers=user_headers,
        )
        if history_response.status_code != 200:
            raise LiveAssertionError("formal v2 history was not readable")
        history = _json_response(history_response)
        history_items = history.get("items")
        if not isinstance(history_items, list) or len(history_items) < 4:
            raise LiveAssertionError("formal v2 history did not retain both rounds")
        if sum(item.get("role") == "user" for item in history_items) < 2:
            raise LiveAssertionError("formal v2 history is missing user messages")

        citation_id = citations[0]["citationId"]
        citation_response = client.get(
            f"{gateway}/enterprise/api/v2/citations/{quote(citation_id, safe='-._~')}",
            headers=user_headers,
        )
        if citation_response.status_code != 200:
            raise LiveAssertionError("citation detail was not readable")
        citation = _json_response(citation_response)
        if (
            citation.get("externalDocumentId") != external_document_id
            or citation.get("sourceVersionId") != source_version_id
        ):
            raise LiveAssertionError("citation detail scope does not match document")

        source_response = client.get(
            f"{gateway}/enterprise/api/v2/citations/{quote(citation_id, safe='-._~')}/source",
            headers=user_headers,
        )
        if source_response.status_code != 200 or hashlib.sha256(source_response.content).hexdigest() != source_sha256:
            raise LiveAssertionError("citation source is not the ingested FILE_SHARE bytes")

    return {
        "fileShareRegistration": True,
        "serverStatusUrl": True,
        "retrievable": True,
        "statusTruthFields": True,
        "formalConversation": True,
        "formalMessages": True,
        "history": True,
        "citationDetail": True,
        "citationSource": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Required FILE_SHARE/v2 live E2E")
    parser.add_argument("--report", type=Path)
    return parser


def _write_report(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_live()
        payload = {"profile": "Integration", "passed": True, "evidence": evidence}
        _write_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except LiveEnvironmentError as exc:
        payload = {
            "profile": "Integration",
            "passed": False,
            "outcome": "environment_missing_or_unavailable",
            "reason": str(exc),
        }
        _write_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 3
    except LiveAssertionError as exc:
        payload = {
            "profile": "Integration",
            "passed": False,
            "outcome": "test_failure",
            "reason": str(exc),
        }
        _write_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 1
    except Exception:
        payload = {"profile": "Integration", "passed": False, "outcome": "runner_failure"}
        _write_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
