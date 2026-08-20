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
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.gateway.auth.service_auth import sign_request  # noqa: E402


class LiveEnvironmentError(RuntimeError):
    """The required live environment is not configured or reachable."""


class LiveAssertionError(RuntimeError):
    """The required live contract returned an unexpected result."""


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if required and not value and not default:
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


def parse_sse(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_name = ""
    data_lines: list[str] = []
    for line in [*payload.splitlines(), ""]:
        if line.startswith(":"):
            continue
        if not line:
            if event_name:
                try:
                    data = json.loads("\n".join(data_lines))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise LiveAssertionError("SSE event returned invalid JSON data") from exc
                if not isinstance(data, dict):
                    raise LiveAssertionError("SSE event returned non-object data")
                events.append((event_name, data))
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return events


def assert_no_internal_grounding(payload: object) -> None:
    if isinstance(payload, dict):
        forbidden = {"grounding", "groundingVersion", "effectiveKnowledge"}
        if forbidden.intersection(payload):
            raise LiveAssertionError("internal grounding metadata leaked externally")
        for value in payload.values():
            assert_no_internal_grounding(value)
    elif isinstance(payload, list):
        for value in payload:
            assert_no_internal_grounding(value)


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _ragflow_base_url() -> str:
    return _env(
        "ENTERPRISE_RAGFLOW_BASE_URL",
        required=False,
        default=_env("RAGFLOW_BASE_URL", required=False, default="http://127.0.0.1:9380"),
    ).rstrip("/")


def _ragflow_api_key() -> str:
    return _env("ENTERPRISE_RAGFLOW_API_KEY", required=False) or _env(
        "RAGFLOW_API_KEY", required=False
    )


def _ragflow_headers() -> dict[str, str]:
    api_key = _ragflow_api_key()
    if not api_key:
        raise LiveEnvironmentError("RAGFlow API key is missing")
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _conversation_mapping(
    db_path: str, conversation_id: str
) -> tuple[str | None, str | None]:
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        row = db.execute(
            """SELECT ragflow_chat_id, ragflow_session_id
               FROM ext_v2_conversation WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _document_scope(
    db_path: str,
    *,
    tenant_id: str,
    external_document_id: str,
    source_version_id: str,
) -> tuple[str | None, str | None]:
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        row = db.execute(
            """SELECT ragflow_dataset_id, ragflow_document_id
               FROM ext_document_map
               WHERE tenant_id=? AND external_document_id=? AND source_version_id=?""",
            (tenant_id, external_document_id, source_version_id),
        ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _assert_ragflow_session(
    db_path: str,
    conversation_id: str,
    questions: tuple[str, ...],
) -> tuple[str, str]:
    chat_id, session_id = _conversation_mapping(db_path, conversation_id)
    if not isinstance(session_id, str) or not session_id.strip():
        raise LiveAssertionError("formal v2 did not persist a RAGFlow session")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise LiveAssertionError("formal v2 did not persist a RAGFlow chat")
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_ragflow_base_url()}/api/v1/chats/{quote(chat_id, safe='')}"
            f"/sessions/{quote(session_id, safe='')}",
            headers=_ragflow_headers(),
        )
    if response.status_code != 200:
        raise LiveAssertionError("RAGFlow session was not readable")
    payload = _json_response(response)
    if payload.get("code") not in (0, None):
        raise LiveAssertionError("RAGFlow session lookup returned an error")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    messages = data.get("messages") or data.get("message") or []
    if not isinstance(messages, list):
        raise LiveAssertionError("RAGFlow session history is missing")
    contents = [
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, dict)
    ]
    joined = "\n".join(contents)
    for question in questions:
        if question not in joined:
            raise LiveAssertionError("RAGFlow session history does not include the live turns")
    return chat_id, session_id


def _chat_retrieval_knobs(chat_id: str) -> dict[str, object]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_ragflow_base_url()}/api/v1/chats/{quote(chat_id, safe='')}",
            headers=_ragflow_headers(),
        )
    if response.status_code != 200:
        raise LiveAssertionError("enterprise Chat retrieval knobs were not readable")
    payload = _json_response(response)
    if payload.get("code") not in (0, None):
        raise LiveAssertionError("enterprise Chat lookup returned an error")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "top_n": data.get("top_n"),
        "top_k": data.get("top_k"),
        "similarity_threshold": data.get("similarity_threshold"),
        "vector_similarity_weight": data.get("vector_similarity_weight"),
        "rerank_id": data.get("rerank_id") or "",
        "dataset_ids": list(data.get("dataset_ids") or []),
    }


def _ranked_retrieval_snapshot(
    *,
    question: str,
    knobs: dict[str, object],
    dataset_id: str,
    document_id: str,
) -> list[dict[str, object]]:
    body: dict[str, object] = {
        "question": question,
        "dataset_ids": [dataset_id],
        "document_ids": [document_id],
        "page": 1,
        "page_size": 8,
    }
    if knobs.get("similarity_threshold") is not None:
        body["similarity_threshold"] = knobs["similarity_threshold"]
    if knobs.get("vector_similarity_weight") is not None:
        body["vector_similarity_weight"] = knobs["vector_similarity_weight"]
    if knobs.get("top_k") is not None:
        body["top_k"] = knobs["top_k"]
    if knobs.get("rerank_id"):
        body["rerank_id"] = knobs["rerank_id"]
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{_ragflow_base_url()}/api/v1/retrieval",
            headers=_ragflow_headers(),
            json=body,
        )
    if response.status_code != 200:
        raise LiveAssertionError("offline retrieval snapshot failed")
    payload = _json_response(response)
    if payload.get("code") not in (0, None):
        raise LiveAssertionError("offline retrieval snapshot returned an error")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    ranked: list[dict[str, object]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        ranked.append(
            {
                "doc_id": chunk.get("document_id") or chunk.get("doc_id"),
                "chunk_id": chunk.get("id") or chunk.get("chunk_id"),
                "score": chunk.get("similarity", chunk.get("score")),
            }
        )
    return ranked


def _write_retrieval_baseline(
    *,
    chat_id: str,
    questions: tuple[str, ...],
    dataset_id: str,
    document_id: str,
    knobs: dict[str, object],
    ranked: list[dict[str, object]],
) -> Path:
    artifact_dir = ROOT / "artifacts" / "e2e"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "file-share-retrieval-baseline.json"
    payload = {
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "gitCommit": _git_head(),
        "chatId": chat_id,
        "datasetId": dataset_id,
        "documentId": document_id,
        "questions": list(questions),
        "chatRetrieval": {
            "top_n": knobs.get("top_n"),
            "top_k": knobs.get("top_k"),
            "similarity_threshold": knobs.get("similarity_threshold"),
            "vector_similarity_weight": knobs.get("vector_similarity_weight"),
            "rerank_id": knobs.get("rerank_id") or "",
        },
        "rankedChunks": ranked,
        "metrics": {
            "recall_at_8": None,
            "mrr": None,
            "misattribution_rate": None,
        },
        "notes": (
            "Record-only baseline. Knobs and ranked ids were not tuned. "
            "Recall metrics stay null without a labeled set."
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def run_live() -> dict[str, object]:
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
    equipment_id = (
        f"{_env('ENTERPRISE_E2E_EQUIPMENT_ID')[:96]}-e2e-{uuid.uuid4().hex[:12]}"
    )
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
            try:
                error_code = str(response.json().get("code") or "unknown")
            except (AttributeError, ValueError, json.JSONDecodeError):
                error_code = "non_json"
            raise LiveAssertionError(
                f"FILE_SHARE registration returned {response.status_code} "
                f"({error_code}), expected 202"
            )
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
            try:
                error_code = str(conversation_response.json().get("code") or "unknown")
            except (AttributeError, ValueError, json.JSONDecodeError):
                error_code = "non_json"
            raise LiveAssertionError(
                f"formal v2 conversation returned {conversation_response.status_code} "
                f"({error_code}), expected 201"
            )
        conversation = _json_response(conversation_response)
        conversation_id = conversation.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise LiveAssertionError("formal v2 conversationId is missing")

        citations: list[dict] = []
        questions = (
            _env(
                "ENTERPRISE_E2E_QUESTION_ONE",
                required=False,
                default="What is RAGFlow designed to turn raw documents into?",
            ),
            _env(
                "ENTERPRISE_E2E_QUESTION_TWO",
                required=False,
                default="Which document formats beyond plain text are explicitly listed?",
            ),
        )
        message_url = (
            f"{gateway}/enterprise/api/v2/conversations/"
            f"{quote(conversation_id, safe='-._~')}/messages"
        )
        generation_timeout = float(os.environ.get("ENTERPRISE_E2E_QUERY_TIMEOUT", "120"))
        json_response = client.post(
            message_url,
            headers=user_headers,
            json={"clientMessageId": "e2e-message-1", "question": questions[0]},
            timeout=generation_timeout,
        )
        if json_response.status_code != 200:
            raise LiveAssertionError("formal v2 JSON message was not completed")
        json_message = _json_response(json_response)
        assert_no_internal_grounding(json_message)
        if not json_message.get("answer") or json_message.get("status") != "已完成":
            raise LiveAssertionError("formal v2 JSON message has no completed answer")
        json_citations = json_message.get("citations")
        matching_json_citations = matching_ingested_citations(
            json_citations,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
        )
        if not matching_json_citations:
            raise LiveAssertionError("JSON citation scope does not include ingested document")
        citations.extend(matching_json_citations)

        sse_response = client.post(
            message_url,
            headers={**user_headers, "Accept": "text/event-stream"},
            json={"clientMessageId": "e2e-message-2", "question": questions[1]},
            timeout=generation_timeout,
        )
        if sse_response.status_code != 200 or not sse_response.headers.get(
            "content-type", ""
        ).startswith("text/event-stream"):
            raise LiveAssertionError("formal v2 SSE message was not completed")
        events = parse_sse(sse_response.text)
        if not events or events[0][0] != "run.started":
            raise LiveAssertionError("formal v2 SSE did not start a run")
        if any(event == "reasoning.delta" for event, _ in events):
            raise LiveAssertionError("grounded SSE exposed reasoning")
        answer_deltas = [data for event, data in events if event == "answer.delta"]
        if len(answer_deltas) != 1 or not answer_deltas[0].get("content"):
            raise LiveAssertionError("grounded SSE did not emit one safe answer delta")
        completed = [data for event, data in events if event == "answer.completed"]
        if len(completed) != 1 or completed[0].get("status") != "已完成":
            raise LiveAssertionError("grounded SSE has no completed terminal event")
        assert_no_internal_grounding([data for _, data in events])
        sse_citations = completed[0].get("citations")
        matching_sse_citations = matching_ingested_citations(
            sse_citations,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
        )
        if not matching_sse_citations:
            raise LiveAssertionError("SSE citation scope does not include ingested document")
        citations.extend(matching_sse_citations)

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

        chat_id, _session_id = _assert_ragflow_session(
            db_path, conversation_id, questions
        )
        dataset_id, document_id = _document_scope(
            db_path,
            tenant_id=tenant_id,
            external_document_id=external_document_id,
            source_version_id=source_version_id,
        )
        if not dataset_id or not document_id:
            raise LiveAssertionError("ingested document is missing RAGFlow dataset/doc ids")
        knobs = _chat_retrieval_knobs(chat_id)
        ranked = _ranked_retrieval_snapshot(
            question=questions[0],
            knobs=knobs,
            dataset_id=dataset_id,
            document_id=document_id,
        )
        baseline_path = _write_retrieval_baseline(
            chat_id=chat_id,
            questions=questions,
            dataset_id=dataset_id,
            document_id=document_id,
            knobs=knobs,
            ranked=ranked,
        )

    return {
        "fileShareRegistration": True,
        "serverStatusUrl": True,
        "retrievable": True,
        "statusTruthFields": True,
        "formalConversation": True,
        "formalMessages": True,
        "groundedSse": True,
        "ragflowSession": True,
        "history": True,
        "citationDetail": True,
        "citationSource": True,
        "retrievalBaseline": True,
        "retrievalBaselinePath": str(baseline_path),
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
