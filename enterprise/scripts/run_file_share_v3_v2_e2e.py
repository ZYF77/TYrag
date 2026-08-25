"""Fail-closed HTTP acceptance for FILE_SHARE 3.1, Query 2.9 and Callback 1.0.

It does not start services or load .env.  Service peers must be literal
loopback addresses by default. Docker mode permits only the two production
Compose service names. Artifacts contain metadata only, never request bodies,
Authorization, signatures, secrets, prompts, or model responses.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from enterprise.gateway.auth.service_auth import sign_request  # noqa: E402

FILE_SHARE_CONTRACT_VERSION = "3.1.0"
QUERY_CONTRACT_VERSION = "2.9.0"
CALLBACK_CONTRACT_VERSION = "1.0.0"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
DOCKER_CALLBACK_HOST = "host.docker.internal"
DOCKER_SERVICE_HOSTS = {"enterprise-gateway", "ragflow-cpu"}
HASH_CHUNK_BYTES = 1024 * 1024


class LiveEnvironmentError(RuntimeError):
    pass


class LiveAssertionError(RuntimeError):
    pass


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if required and not value and not default:
        raise LiveEnvironmentError("required_local_configuration_missing")
    return value or default


def assert_http_target(value: str, *, target_mode: str = "local", callback: bool = False) -> str:
    """Reject every HTTP target outside the selected fixed allowlist."""
    parsed = urlsplit(value)
    allowed = set(LOOPBACK_HOSTS)
    if target_mode == "docker" and not callback:
        allowed |= DOCKER_SERVICE_HOSTS
    elif target_mode != "local":
        raise LiveEnvironmentError("invalid_target_mode")
    if callback:
        allowed.add(DOCKER_CALLBACK_HOST)
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not parsed.hostname or parsed.hostname.lower() not in allowed):
        raise LiveEnvironmentError("http_target_rejected")
    return value.rstrip("/")


def assert_local_http_target(value: str, *, callback: bool = False) -> str:
    return assert_http_target(value, callback=callback)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise LiveAssertionError("endpoint_returned_non_json") from exc
    if not isinstance(value, dict):
        raise LiveAssertionError("endpoint_returned_invalid_json")
    return value


def _ragflow_data(response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise LiveAssertionError("ragflow_public_api_unavailable")
    payload = _json(response)
    if payload.get("code") not in (0, None):
        raise LiveAssertionError("ragflow_public_api_error")
    return payload.get("data", payload)


def _load_hmac_credential(tenant_id: str, source_system: str) -> tuple[str, str]:
    try:
        raw = json.loads(_env("ENTERPRISE_SYNC_HMAC_CREDENTIALS"))
        items = raw.get("credentials", [raw]) if isinstance(raw, dict) else raw
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveEnvironmentError("hmac_credentials_invalid") from exc
    if not isinstance(items, list):
        raise LiveEnvironmentError("hmac_credentials_invalid")
    for item in items:
        if not isinstance(item, dict) or item.get("status", "active") not in {"active", "previous"}:
            continue
        for binding in item.get("allowedBindings", item.get("allowed_bindings", [])):
            if (isinstance(binding, dict)
                    and binding.get("tenantId", binding.get("tenant_id")) == tenant_id
                    and binding.get("sourceSystem", binding.get("source_system")) == source_system):
                key_id, secret = item.get("keyId", item.get("key_id")), item.get("secret")
                if isinstance(key_id, str) and key_id and isinstance(secret, str) and secret:
                    return key_id, secret
    raise LiveEnvironmentError("hmac_credential_not_bound_to_scope")


def _safe_relative_path(relative_path: str) -> Path:
    path = Path(relative_path)
    if not relative_path or path.is_absolute() or ".." in path.parts:
        raise LiveEnvironmentError("file_share_relative_path_invalid")
    return path


def _file_path(root_id: str, relative_path: str) -> Path:
    root_text = os.environ.get("ENTERPRISE_FILE_SHARE_LOCAL_ROOT", "").strip()
    if not root_text:
        try:
            roots = json.loads(_env("ENTERPRISE_FILE_SHARE_ROOTS"))
            root_text = roots.get(root_id, "") if isinstance(roots, dict) else ""
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            raise LiveEnvironmentError("file_share_roots_invalid") from exc
    root = Path(root_text).resolve()
    path = (root / _safe_relative_path(relative_path)).resolve()
    if root not in path.parents or not path.is_file():
        raise LiveEnvironmentError("file_share_test_document_unavailable")
    return path


def _stage_unique_source_copy(source_path: Path, relative_path: str) -> tuple[Path, str]:
    safe_path = _safe_relative_path(relative_path)
    staged = source_path.with_name(f"{source_path.stem}-local-e2e-{uuid.uuid4().hex[:12]}{source_path.suffix}")
    shutil.copyfile(source_path, staged)
    return staged, safe_path.with_name(staged.name).as_posix()


def _sha256_file(path: Path, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> tuple[str, int]:
    if not 0 < chunk_bytes <= HASH_CHUNK_BYTES:
        raise ValueError("hash_chunk_size_out_of_bounds")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(chunk_bytes):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        count = len(PdfReader(path).pages)
    except ModuleNotFoundError:
        count = len(re.findall(rb"/Type\s*/Page(?!s)\b", path.read_bytes()))
    if count < 1:
        raise LiveEnvironmentError("pdf_page_count_unavailable")
    return count


def _metadata(*, tenant_id: str, source_system: str, external_document_id: str,
              source_version_id: str, equipment_id: str, fixed_asset_no: str,
              page_count: int = 1) -> dict[str, Any]:
    return {"schema_version": 1, "tenant_id": tenant_id, "external_document_id": external_document_id,
            "source_system": source_system, "equipment_id": equipment_id,
            "fixed_asset_no": fixed_asset_no or None, "document_type": os.getenv("ENTERPRISE_E2E_DOCUMENT_TYPE", "PRODUCT_MANUAL"),
            "document_version": source_version_id, "department_id": os.getenv("ENTERPRISE_E2E_DEPARTMENT_ID", "maintenance"),
            "security_level": int(os.getenv("ENTERPRISE_E2E_SECURITY_LEVEL", "2")), "business_status": "active",
            "allow_group_ids": [x.strip() for x in os.getenv("ENTERPRISE_E2E_ALLOW_GROUPS", "maintenance").split(",") if x.strip()],
            "deny_group_ids": [], "page_count": page_count}


def _service_headers(*, key_id: str, secret: str, method: str, relative_url: str,
                     body: bytes = b"") -> dict[str, str]:
    parsed, timestamp = urlsplit(relative_url), str(int(time.time()))
    return {"Accept": "application/json", "Content-Type": "application/json", "X-TY-Timestamp": timestamp,
            "X-TY-Key-Id": key_id, "X-TY-Signature": sign_request(secret=secret, timestamp=timestamp,
                method=method, path=parsed.path, query=parsed.query, body=body)}


def _jwt_token(*, secret: str, issuer: str, audience: str, subject: str, tenant_id: str,
               groups: list[str] | None = None) -> str:
    now = int(time.time())
    return jwt.encode({"sub": subject, "tenant": tenant_id, "business_user_id": subject, "name": subject,
        "department": [os.getenv("ENTERPRISE_E2E_DEPARTMENT_ID", "maintenance")], "roles": ["end_user"],
        "groups": groups if groups is not None else [x.strip() for x in os.getenv("ENTERPRISE_E2E_USER_GROUPS", "maintenance").split(",") if x.strip()],
        "security_level": int(os.getenv("ENTERPRISE_E2E_SECURITY_LEVEL", "2")), "iat": now - 5, "exp": now + 900,
        "iss": issuer, "aud": audience}, secret, algorithm="HS256")


def build_diagnostic_status_url(*, tenant_id: str, source_system: str,
                                external_document_id: str, source_version_id: str) -> str:
    return (f"/enterprise/api/v3/documents/{quote(external_document_id, safe='')}/status?"
            f"tenantId={quote(tenant_id, safe='')}&sourceSystem={quote(source_system, safe='')}&"
            f"sourceVersionId={quote(source_version_id, safe='')}")


def validate_accept_receipt(payload: dict, *, external_document_id: str, source_version_id: str) -> None:
    required = {"operationId", "externalDocumentId", "sourceVersionId", "deduplicated", "updatedAt"}
    if not required <= set(payload) or "statusUrl" in payload:
        raise LiveAssertionError("invalid_accept_receipt")
    if payload.get("externalDocumentId") != external_document_id or payload.get("sourceVersionId") != source_version_id:
        raise LiveAssertionError("accept_receipt_identity_mismatch")


def matching_ingested_citations(citations: object, *, external_document_id: str,
                                source_version_id: str) -> list[dict]:
    return [item for item in citations if isinstance(item, dict) and item.get("citationId")
            and item.get("externalDocumentId") == external_document_id
            and item.get("sourceVersionId") == source_version_id] if isinstance(citations, list) else []


def assert_no_internal_grounding(payload: object) -> None:
    if isinstance(payload, dict):
        if {"grounding", "groundingVersion", "effectiveKnowledge"} & set(payload):
            raise LiveAssertionError("internal_grounding_leaked")
        for value in payload.values():
            assert_no_internal_grounding(value)
    elif isinstance(payload, list):
        for value in payload:
            assert_no_internal_grounding(value)


def parse_sse(payload: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse only named JSON SSE events; response text is never persisted."""
    events: list[tuple[str, dict[str, Any]]] = []
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
                    raise LiveAssertionError("sse_event_invalid_json") from exc
                if not isinstance(data, dict):
                    raise LiveAssertionError("sse_event_invalid_payload")
                events.append((event_name, data))
            event_name, data_lines = "", []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return events


def _assert_sse_turn(events: list[tuple[str, dict[str, Any]]], *,
                     external_document_id: str, source_version_id: str) -> list[dict]:
    if not events or events[0][0] != "run.started":
        raise LiveAssertionError("sse_run_not_started")
    if not any(name == "answer.delta" and data.get("content") for name, data in events):
        raise LiveAssertionError("sse_answer_delta_missing")
    completed = [data for name, data in events if name == "answer.completed"]
    if len(completed) != 1 or completed[0].get("status") != "已完成":
        raise LiveAssertionError("sse_terminal_event_invalid")
    assert_no_internal_grounding([data for _, data in events])
    citations = matching_ingested_citations(
        completed[0].get("citations"),
        external_document_id=external_document_id,
        source_version_id=source_version_id,
    )
    if not citations:
        raise LiveAssertionError("sse_citation_scope_missing")
    return completed


def _assert_history_replay(payload: dict[str, Any], *, json_status: object,
                           sse_status: object) -> None:
    items = payload.get("items")
    if not isinstance(items, list):
        raise LiveAssertionError("history_items_missing")
    roles = [item.get("role") for item in items if isinstance(item, dict)]
    if roles.count("user") < 2 or roles.count("assistant") < 2:
        raise LiveAssertionError("history_did_not_retain_two_turns")
    assistant_statuses = [item.get("status") for item in items
                          if isinstance(item, dict) and item.get("role") == "assistant"]
    allowed = {"已完成", "无可靠依据", "失败"}
    if any(status not in allowed for status in assistant_statuses):
        raise LiveAssertionError("history_business_status_invalid")
    if json_status not in assistant_statuses or sse_status not in assistant_statuses:
        raise LiveAssertionError("history_business_status_not_replayed")


@dataclass
class Artifacts:
    directory: Path
    requests: list[dict[str, Any]] = field(default_factory=list)
    callbacks: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, method: str, url: str, status: int | None) -> None:
        self.requests.append({"name": name, "method": method, "target": urlsplit(url).path, "httpStatus": status})

    def write(self, summary: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.directory / "requests.ndjson").write_text("".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in self.requests), encoding="utf-8")
        (self.directory / "callback-evidence.json").write_text(json.dumps({"contractVersion": CALLBACK_CONTRACT_VERSION, "events": self.callbacks}, ensure_ascii=False, indent=2), encoding="utf-8")
        outcome = summary.get("outcome", "passed" if summary.get("passed") else "unknown")
        (self.directory / "acceptance.md").write_text(f"# FILE_SHARE local acceptance\n\nOutcome: {outcome}\n\nFILE_SHARE {FILE_SHARE_CONTRACT_VERSION}; Query {QUERY_CONTRACT_VERSION}; Callback {CALLBACK_CONTRACT_VERSION}.\n", encoding="utf-8")


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        server: CallbackReceiver = self.server.receiver  # type: ignore[attr-defined]
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, json.JSONDecodeError):
            self.send_response(400); self.end_headers(); return
        self.send_response(server.accept(payload, self.headers.get("X-TY-Timestamp", ""), self.headers.get("X-TY-Signature", "")))
        self.end_headers()


class CallbackReceiver:
    """Local callback receiver: the positive callback gets 503 once then 204."""
    def __init__(self, url: str, secret: str, expected_document: str, artifacts: Artifacts):
        parsed = urlsplit(assert_local_http_target(url, callback=True))
        if parsed.scheme != "http":
            raise LiveEnvironmentError("callback_listener_requires_http")
        self.secret, self.expected_document, self.artifacts = secret, expected_document, artifacts
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = _CallbackHTTPServer(("127.0.0.1", parsed.port or 80), _CallbackHandler)
        self.server.receiver = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "CallbackReceiver":
        self.thread.start(); return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def accept(self, payload: object, timestamp: str, signature: str) -> int:
        if not isinstance(payload, dict) or not timestamp.isdigit() or not signature.startswith("sha256="):
            return 401
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        expected = "sha256=" + hmac.new(self.secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return 401
        doc, delivery = payload.get("externalDocumentId"), payload.get("deliveryId")
        if not isinstance(doc, str) or not isinstance(delivery, str):
            return 400
        with self.lock:
            count = sum(x["externalDocumentId"] == doc for x in self.events)
            status = 503 if doc == self.expected_document and count == 0 else 204
            event = {"deliveryId": delivery, "externalDocumentId": doc, "sourceVersionId": payload.get("sourceVersionId"), "httpStatus": status}
            self.events.append(event); self.artifacts.callbacks.append(event)
        return status

    def assert_success(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                events = [x for x in self.events if x["externalDocumentId"] == self.expected_document]
            if len(events) >= 2:
                if [x["httpStatus"] for x in events[:2]] != [503, 204] or events[0]["deliveryId"] != events[1]["deliveryId"]:
                    raise LiveAssertionError("callback_retry_contract_failed")
                return
            time.sleep(.2)
        raise LiveAssertionError("callback_retry_not_observed")


class ExistingCallbackDelivery:
    """Observe the configured callback worker through non-sensitive DB fields."""

    def __init__(self, db_path: str, tenant: str, system: str, document: str,
                 version: str, artifacts: Artifacts):
        self.db_path = db_path
        self.scope = (tenant, system, document, version)
        self.artifacts = artifacts

    def __enter__(self) -> "ExistingCallbackDelivery":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def assert_success(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        query = """SELECT delivery_id,attempts,state,last_http_status
                   FROM callback_delivery
                   WHERE tenant_id=? AND source_system=? AND external_document_id=?
                     AND source_version_id=? ORDER BY id DESC LIMIT 1"""
        while time.monotonic() < deadline:
            with sqlite3.connect(Path(self.db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute(query, self.scope).fetchone()
            if row and row[2] == "delivered":
                self.artifacts.callbacks.append({
                    "deliveryId": row[0], "attempts": row[1], "httpStatus": row[3],
                })
                return
            if row and row[2] == "dead_letter":
                raise LiveAssertionError("configured_callback_delivery_failed")
            time.sleep(.2)
        raise LiveAssertionError("configured_callback_delivery_not_observed")


def _document_scope(db_path: str, tenant: str, document: str, version: str) -> tuple[str | None, str | None]:
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT ragflow_dataset_id,ragflow_document_id FROM ext_document_map WHERE tenant_id=? AND external_document_id=? AND source_version_id=?", (tenant, document, version)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _document_equipment(db_path: str, tenant: str, document: str, version: str) -> str | None:
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT equipment_id FROM ext_document_map WHERE tenant_id=? AND external_document_id=? AND source_version_id=?", (tenant, document, version)).fetchone()
    return row[0] if row else None


def _assert_gateway_did_not_own_parser(
    db_path: str, tenant: str, document: str, version: str,
) -> None:
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            """SELECT parser_profile,parser_profile_version,parser_expected_json,
                      parser_configured_json,parser_executed_json,parse_retry_count
                 FROM ext_document_map
                WHERE tenant_id=? AND external_document_id=? AND source_version_id=?""",
            (tenant, document, version),
        ).fetchone()
    if not row or any(value is not None for value in row[:5]):
        raise LiveAssertionError("gateway_parser_ownership_not_removed")
    if int(row[5] or 0) != 0:
        raise LiveAssertionError("normal_document_unexpected_parse_retry")


def _post_feed(client: httpx.Client, gateway: str, key: str, secret: str, payload: dict, artifacts: Artifacts, name: str) -> httpx.Response:
    body, path = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(), "/enterprise/api/v3/documents"
    response = client.post(gateway + path, content=body, headers=_service_headers(key_id=key, secret=secret, method="POST", relative_url=path, body=body))
    artifacts.record(name, "POST", gateway + path, response.status_code)
    return response


def _poll_status(client: httpx.Client, gateway: str, key: str, secret: str, path: str,
                 artifacts: Artifacts, expected_failure: bool = False) -> None:
    for _ in range(int(os.getenv("ENTERPRISE_E2E_STATUS_ATTEMPTS", "120"))):
        response = client.get(gateway + path, headers=_service_headers(key_id=key, secret=secret, method="GET", relative_url=path))
        artifacts.record("status_failure" if expected_failure else "status_quality_gate", "GET", gateway + path, response.status_code)
        if response.status_code == 200:
            status = _json(response)
            if expected_failure and status.get("retrievable") is not True and (status.get("errorCode") is not None or status.get("error") or str(status.get("status", "")).lower() in {"failed", "review_required", "unavailable"}):
                return
            readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
            if not expected_failure and status.get("retrievable") is True:
                if (str(status.get("pipelineStatus", "")).upper() not in {"DONE", "3"}
                        or status.get("parseCompleted") is not True or status.get("indexCompleted") is not True
                        or status.get("qualityStatus") != "passed" or status.get("errorCode") is not None
                        or readiness.get("qualityPassed") is not True):
                    raise LiveAssertionError("quality_gate_or_parse_facts_missing")
                return
        elif response.status_code not in {404, 409, 422}:
            raise LiveAssertionError("status_response_unexpected")
        time.sleep(float(os.getenv("ENTERPRISE_E2E_STATUS_INTERVAL", "2")))
    raise LiveAssertionError("terminal_status_not_observed")


def _verify_native_upload(client: httpx.Client, ragflow: str, api_key: str, dataset: str,
                          document: str, artifacts: Artifacts) -> None:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    url = f"{ragflow}/api/v1/datasets/{quote(dataset, safe='')}/documents?id={quote(document, safe='')}"
    response = client.get(url, headers=headers); artifacts.record("ragflow_native_document", "GET", url, response.status_code)
    data = _ragflow_data(response)
    docs = data.get("docs", data.get("data", [])) if isinstance(data, dict) else []
    doc = next((x for x in docs if isinstance(x, dict) and x.get("id") == document), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("location"), str) or not doc["location"] or doc["location"].startswith("external://"):
        raise LiveAssertionError("ragflow_document_not_native_upload")
    expected_method = os.getenv("ENTERPRISE_E2E_EXPECTED_CHUNK_METHOD", "").strip()
    if expected_method and doc.get("chunk_method") != expected_method:
        raise LiveAssertionError("ragflow_chunk_method_was_overridden")
    expected_layout = os.getenv("ENTERPRISE_E2E_EXPECTED_LAYOUT_RECOGNIZE", "").strip()
    parser_config = doc.get("parser_config") if isinstance(doc.get("parser_config"), dict) else {}
    if expected_layout and parser_config.get("layout_recognize") != expected_layout:
        raise LiveAssertionError("ragflow_parser_config_was_overridden")
    chunks_url = f"{ragflow}/api/v1/datasets/{quote(dataset, safe='')}/documents/{quote(document, safe='')}/chunks?page=1&page_size=10"
    chunks_response = client.get(chunks_url, headers=headers); artifacts.record("ragflow_chunks_parse", "GET", chunks_url, chunks_response.status_code)
    chunks_data = _ragflow_data(chunks_response)
    chunks = chunks_data.get("chunks", chunks_data.get("data", [])) if isinstance(chunks_data, dict) else []
    if not isinstance(chunks, list) or not chunks:
        raise LiveAssertionError("ragflow_chunks_parse_evidence_missing")


def _preflight_services(client: httpx.Client, *, gateway: str, ragflow: str,
                        api_key: str, artifacts: Artifacts) -> None:
    health_url = gateway + "/enterprise/api/v1/health"
    health = client.get(health_url)
    artifacts.record("gateway_health", "GET", health_url, health.status_code)
    if health.status_code != 200:
        raise LiveEnvironmentError("gateway_health_unavailable")
    version_url = ragflow + "/api/v1/system/version"
    version = client.get(version_url, headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"})
    artifacts.record("ragflow_version", "GET", version_url, version.status_code)
    data = _ragflow_data(version)
    actual = data.get("version") if isinstance(data, dict) else data
    if actual != "v0.26.4":
        raise LiveEnvironmentError("ragflow_version_not_v0_26_4")


def _verify_ragflow_session(client: httpx.Client, *, ragflow: str, api_key: str,
                            chat_id: str, session_id: str, artifacts: Artifacts) -> None:
    url = f"{ragflow}/api/v1/chats/{quote(chat_id, safe='')}/sessions/{quote(session_id, safe='')}"
    response = client.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"})
    artifacts.record("ragflow_session", "GET", url, response.status_code)
    data = _ragflow_data(response)
    session = data if isinstance(data, dict) else {}
    messages = session.get("messages") or session.get("message") or []
    if not isinstance(messages, list) or len(messages) < 4:
        raise LiveAssertionError("ragflow_session_history_missing")


def _find_ragflow_session(client: httpx.Client, *, ragflow: str, api_key: str,
                          tenant: str, conversation_id: str,
                          artifacts: Artifacts) -> tuple[str, str]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    chat_name = f"enterprise-formal-{tenant}"
    chats_url = f"{ragflow}/api/v1/chats?name={quote(chat_name, safe='')}&page=1&page_size=10"
    chats_response = client.get(chats_url, headers=headers)
    artifacts.record("ragflow_chat_lookup", "GET", chats_url, chats_response.status_code)
    chats_data = _ragflow_data(chats_response)
    chats = chats_data.get("chats", []) if isinstance(chats_data, dict) else []
    chat_matches = [
        item for item in chats
        if isinstance(item, dict) and item.get("name") == chat_name and item.get("id")
    ]
    if len(chat_matches) != 1:
        raise LiveAssertionError("ragflow_chat_mapping_missing")
    chat_id = str(chat_matches[0]["id"])
    sessions_url = (
        f"{ragflow}/api/v1/chats/{quote(chat_id, safe='')}/sessions"
        "?page=1&page_size=100&orderby=create_time&desc=true"
    )
    sessions_response = client.get(sessions_url, headers=headers)
    artifacts.record("ragflow_session_lookup", "GET", sessions_url, sessions_response.status_code)
    sessions = _ragflow_data(sessions_response)
    session_matches = [
        item for item in sessions
        if isinstance(item, dict)
        and conversation_id in str(item.get("name") or "")
        and item.get("id")
    ] if isinstance(sessions, list) else []
    if len(session_matches) != 1:
        raise LiveAssertionError("ragflow_session_mapping_missing")
    return chat_id, str(session_matches[0]["id"])


def _verify_document_scoped_retrieval(client: httpx.Client, *, ragflow: str,
                                      api_key: str, dataset_id: str, document_id: str,
                                      artifacts: Artifacts) -> None:
    url = ragflow + "/api/v1/retrieval"
    response = client.post(url, headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"}, json={
        "question": _env("ENTERPRISE_E2E_RETRIEVAL_QUESTION", required=False,
                         default="Summarize the registered document."),
        "dataset_ids": [dataset_id],
        "document_ids": [document_id],
        "page": 1,
        "page_size": 8,
    })
    artifacts.record("ragflow_document_scoped_retrieval", "POST", url, response.status_code)
    data = _ragflow_data(response)
    chunks = data.get("chunks", []) if isinstance(data, dict) else []
    if not isinstance(chunks, list) or not chunks:
        raise LiveAssertionError("ragflow_document_scoped_retrieval_empty")
    if any(
        not isinstance(chunk, dict)
        or (chunk.get("document_id") or chunk.get("doc_id")) != document_id
        for chunk in chunks
    ):
        raise LiveAssertionError("ragflow_retrieval_not_limited_to_document_id")


def _stream_sha256(client: httpx.Client, url: str, headers: dict[str, str], artifacts: Artifacts) -> str:
    digest = hashlib.sha256()
    with client.stream("GET", url, headers=headers) as response:
        artifacts.record("citation_source", "GET", url, response.status_code)
        if response.status_code != 200:
            raise LiveAssertionError("citation_source_not_authorized")
        for block in response.iter_bytes(chunk_size=HASH_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _large_file_check(root_id: str, artifacts: Artifacts) -> bool:
    if os.getenv("ENTERPRISE_E2E_VERIFY_100MB", "false").lower() not in {"1", "true", "yes"}:
        return False
    _digest, size = _sha256_file(_file_path(root_id, _env("ENTERPRISE_E2E_LARGE_FILE_RELATIVE_PATH")))
    if size <= 100 * 1024 * 1024:
        raise LiveAssertionError("configured_large_file_is_not_over_100mb")
    artifacts.record("bounded_large_file_hash", "LOCAL", "/file-share/large-file", 200)
    return True


def run_live(artifacts: Artifacts, *, target_mode: str = "local",
             callback_mode: str = "temporary", resume_document: str = "") -> dict[str, Any]:
    gateway = assert_http_target(_env("GATEWAY_URL"), target_mode=target_mode)
    ragflow = assert_http_target(_env("ENTERPRISE_RAGFLOW_BASE_URL", required=False, default=_env("RAGFLOW_BASE_URL", required=False, default="http://127.0.0.1:9380")), target_mode=target_mode)
    api_key = _env("ENTERPRISE_RAGFLOW_API_KEY", required=False) or _env("RAGFLOW_API_KEY", required=False)
    if not api_key: raise LiveEnvironmentError("ragflow_api_key_missing")
    tenant, system, root = _env("ENTERPRISE_E2E_TENANT_ID", default="tyrag-integration"), _env("ENTERPRISE_E2E_SOURCE_SYSTEM", default="EAM"), _env("ENTERPRISE_E2E_STORAGE_ROOT_ID", default="device-share")
    version, original_path = _env("ENTERPRISE_E2E_SOURCE_VERSION_ID", required=False, default="v3-1"), _env("ENTERPRISE_E2E_FILE_RELATIVE_PATH", required=False, default="Doc1.pdf")
    document = resume_document or _env("ENTERPRISE_E2E_EXTERNAL_DOCUMENT_ID", required=False, default=f"LOCAL-E2E-{time.time_ns()}")
    if document.upper().startswith(("PROBE-", "TYRAG-E2E-")): raise LiveEnvironmentError("callback_fixture_id_not_allowed")
    db_path = _env("ENTERPRISE_DB_PATH")
    if not Path(db_path).is_file(): raise LiveEnvironmentError("gateway_database_unavailable")
    key, hmac_secret = _load_hmac_credential(tenant, system)
    jwt_secret, issuer, audience = _env("JWT_SHARED_SECRET"), _env("JWT_ISSUER"), _env("JWT_AUDIENCE")
    callback_secret = ""
    if callback_mode == "temporary":
        callback_secret = _env("ENTERPRISE_E2E_CALLBACK_HMAC_SECRET", required=False) or _env("ENTERPRISE_CALLBACK_HMAC_SECRET")
    reuse_source = os.getenv("ENTERPRISE_E2E_USE_EXISTING_SOURCE", "false").lower() in {"1", "true", "yes"}
    if reuse_source:
        staged, relative = _file_path(root, original_path), original_path
    else:
        staged, relative = _stage_unique_source_copy(_file_path(root, original_path), original_path)
    try:
        sha, size = _sha256_file(staged); page_count = _pdf_page_count(staged); stat = staged.stat()
        equipment = (
            _document_equipment(db_path, tenant, document, version)
            if resume_document
            else f"{_env('ENTERPRISE_E2E_EQUIPMENT_ID')[:96]}-e2e-{uuid.uuid4().hex[:12]}"
        )
        if not equipment:
            raise LiveEnvironmentError("existing_document_equipment_missing")
        event = _env("ENTERPRISE_E2E_EVENT_ID", required=False, default=f"evt-{document}")
        feed = {"eventId": event, "eventType": "upsert", "tenantId": tenant, "sourceSystem": system,
                "externalDocumentId": document, "sourceVersionId": version, "sha256": sha, "fileName": staged.name,
                "mediaType": "application/pdf", "source": {"kind": "FILE_SHARE", "storageRootId": root,
                    "relativePath": relative, "size": size, "etag": f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'},
                "metadata": _metadata(tenant_id=tenant, source_system=system, external_document_id=document,
                    source_version_id=version, equipment_id=equipment, fixed_asset_no=_env("ENTERPRISE_E2E_FIXED_ASSET_NO", required=False),
                    page_count=page_count)}
        question_one = _env(
            "ENTERPRISE_E2E_QUESTION",
            required=False,
            default="Summarize the registered document.",
        )
        question_two = _env(
            "ENTERPRISE_E2E_SSE_QUESTION",
            required=False,
            default="Which facts in the registered document support its summary?",
        )
        callback = (
            ExistingCallbackDelivery(db_path, tenant, system, document, version, artifacts)
            if callback_mode == "existing"
            else CallbackReceiver(_env("ENTERPRISE_E2E_CALLBACK_URL"), callback_secret, document, artifacts)
        )
        with callback, httpx.Client(timeout=30, follow_redirects=False) as client:
            _preflight_services(
                client,
                gateway=gateway,
                ragflow=ragflow,
                api_key=api_key,
                artifacts=artifacts,
            )
            if not resume_document:
                accepted = _post_feed(client, gateway, key, hmac_secret, feed, artifacts, "file_share_feed")
                if accepted.status_code != 202: raise LiveAssertionError("file_share_feed_not_accepted")
                receipt = _json(accepted); validate_accept_receipt(receipt, external_document_id=document, source_version_id=version)
                time.sleep(1.05)
                replay = _post_feed(client, gateway, key, hmac_secret, feed, artifacts, "file_share_feed_replay")
                if replay.status_code != 202: raise LiveAssertionError("business_idempotency_replay_not_accepted")
                replay_receipt = _json(replay); validate_accept_receipt(replay_receipt, external_document_id=document, source_version_id=version)
                if replay_receipt.get("deduplicated") is not True or replay_receipt.get("operationId") != receipt.get("operationId"): raise LiveAssertionError("business_idempotency_failed")
                status_url = build_diagnostic_status_url(tenant_id=tenant, source_system=system,
                    external_document_id=document, source_version_id=version)
                _poll_status(client, gateway, key, hmac_secret, status_url, artifacts)
            dataset, rag_document = _document_scope(db_path, tenant, document, version)
            if not dataset or not rag_document: raise LiveAssertionError("ragflow_mapping_missing")
            if not resume_document:
                _assert_gateway_did_not_own_parser(
                    db_path, tenant, document, version,
                )
            _verify_native_upload(client, ragflow, api_key, dataset, rag_document, artifacts)
            traversal_id = f"LOCAL-PATH-{uuid.uuid4().hex}"
            traversal = {
                **feed,
                "eventId": f"evt-path-{uuid.uuid4().hex}",
                "externalDocumentId": traversal_id,
                "source": {**feed["source"], "relativePath": "../outside.pdf"},
                "metadata": _metadata(
                    tenant_id=tenant,
                    source_system=system,
                    external_document_id=traversal_id,
                    source_version_id=version,
                    equipment_id=equipment,
                    fixed_asset_no="",
                ),
            }
            bad_path = _post_feed(client, gateway, key, hmac_secret, traversal, artifacts, "negative_path_traversal")
            if bad_path.status_code == 202:
                _poll_status(
                    client,
                    gateway,
                    key,
                    hmac_secret,
                    build_diagnostic_status_url(
                        tenant_id=tenant,
                        source_system=system,
                        external_document_id=traversal_id,
                        source_version_id=version,
                    ),
                    artifacts,
                    expected_failure=True,
                )
            elif not 400 <= bad_path.status_code < 500:
                raise LiveAssertionError("path_traversal_not_rejected")
            _, traversal_ragflow_document = _document_scope(
                db_path, tenant, traversal_id, version
            )
            if traversal_ragflow_document:
                raise LiveAssertionError("path_traversal_created_ragflow_document")
            mismatch_id = f"LOCAL-SHA-{uuid.uuid4().hex}"
            mismatch = {**feed, "eventId": f"evt-sha-{uuid.uuid4().hex}", "externalDocumentId": mismatch_id, "sha256": "0" * 64,
                "metadata": _metadata(tenant_id=tenant, source_system=system, external_document_id=mismatch_id, source_version_id=version, equipment_id=equipment, fixed_asset_no="")}
            bad_hash = _post_feed(client, gateway, key, hmac_secret, mismatch, artifacts, "negative_sha_mismatch")
            if bad_hash.status_code == 202:
                _poll_status(client, gateway, key, hmac_secret, build_diagnostic_status_url(tenant_id=tenant, source_system=system, external_document_id=mismatch_id, source_version_id=version), artifacts, expected_failure=True)
            elif not 400 <= bad_hash.status_code < 500: raise LiveAssertionError("sha_mismatch_not_rejected")
            _, mismatch_ragflow_document = _document_scope(
                db_path, tenant, mismatch_id, version
            )
            if mismatch_ragflow_document:
                raise LiveAssertionError("sha_mismatch_created_ragflow_document")
            token = _jwt_token(secret=jwt_secret, issuer=issuer, audience=audience, subject=_env("ENTERPRISE_E2E_USER_SUBJECT", required=False, default="tyrag-e2e-user"), tenant_id=tenant)
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            conversation_url = gateway + "/enterprise/api/v2/conversations"; conversation = client.post(conversation_url, headers=headers, json={"equipmentId": equipment})
            artifacts.record("authorized_conversation", "POST", conversation_url, conversation.status_code)
            if conversation.status_code != 201: raise LiveAssertionError("authorized_conversation_not_created")
            conversation_id = _json(conversation).get("conversationId")
            if not isinstance(conversation_id, str) or not conversation_id: raise LiveAssertionError("conversation_id_missing")
            message_url = gateway + f"/enterprise/api/v2/conversations/{quote(conversation_id, safe='-._~')}/messages"
            query = client.post(
                message_url,
                headers=headers,
                json={"clientMessageId": "local-e2e-query", "question": question_one},
                timeout=float(os.getenv("ENTERPRISE_E2E_QUERY_TIMEOUT", "120")),
            )
            artifacts.record("authorized_query", "POST", message_url, query.status_code)
            if query.status_code != 200: raise LiveAssertionError("authorized_query_not_completed")
            result = _json(query); assert_no_internal_grounding(result)
            citations = matching_ingested_citations(result.get("citations"), external_document_id=document, source_version_id=version)
            if not citations: raise LiveAssertionError("query_citation_scope_missing")
            sse = client.post(
                message_url,
                headers={**headers, "Accept": "text/event-stream"},
                json={"clientMessageId": "local-e2e-sse", "question": question_two},
                timeout=float(os.getenv("ENTERPRISE_E2E_QUERY_TIMEOUT", "120")),
            )
            artifacts.record("authorized_sse_query", "POST", message_url, sse.status_code)
            if sse.status_code != 200 or not sse.headers.get("content-type", "").startswith("text/event-stream"):
                raise LiveAssertionError("sse_query_not_completed")
            completed = _assert_sse_turn(
                parse_sse(sse.text),
                external_document_id=document,
                source_version_id=version,
            )
            history_url = message_url
            history = client.get(history_url, headers=headers)
            artifacts.record("conversation_history", "GET", history_url, history.status_code)
            if history.status_code != 200:
                raise LiveAssertionError("conversation_history_not_readable")
            _assert_history_replay(
                _json(history),
                json_status=result.get("status"),
                sse_status=completed[0].get("status"),
            )
            citation_url = gateway + f"/enterprise/api/v2/citations/{quote(citations[0]['citationId'], safe='-._~')}"
            citation = client.get(citation_url, headers=headers); artifacts.record("authorized_citation", "GET", citation_url, citation.status_code)
            if citation.status_code != 200: raise LiveAssertionError("authorized_citation_not_readable")
            source_url = citation_url + "/source"
            if _stream_sha256(client, source_url, headers, artifacts) != sha: raise LiveAssertionError("citation_source_sha256_mismatch")
            denied = _jwt_token(secret=jwt_secret, issuer=issuer, audience=audience, subject="local-e2e-denied", tenant_id=tenant, groups=["local-e2e-denied"])
            forbidden = client.get(source_url, headers={"Authorization": f"Bearer {denied}"}); artifacts.record("negative_acl_overreach", "GET", source_url, forbidden.status_code)
            if forbidden.status_code not in {403, 404}: raise LiveAssertionError("acl_overreach_not_rejected")
            chat_id, session_id = _find_ragflow_session(
                client,
                ragflow=ragflow,
                api_key=api_key,
                tenant=tenant,
                conversation_id=conversation_id,
                artifacts=artifacts,
            )
            _verify_ragflow_session(
                client,
                ragflow=ragflow,
                api_key=api_key,
                chat_id=chat_id,
                session_id=session_id,
                artifacts=artifacts,
            )
            _verify_document_scoped_retrieval(
                client,
                ragflow=ragflow,
                api_key=api_key,
                dataset_id=dataset,
                document_id=rag_document,
                artifacts=artifacts,
            )
            callback.assert_success(float(os.getenv("ENTERPRISE_E2E_CALLBACK_TIMEOUT", "45")))
            large_checked = _large_file_check(root, artifacts)
        summary = {"fileShareFeed": not resume_document, "businessIdempotency": not resume_document,
                   "resumedExistingDocument": bool(resume_document), "nativeUpload": True, "chunksParse": True,
                   "qualityGate": True, "ragflowOwnedParserConfig": True,
                   "callbackRetry": True, "authorizedQueryCitationSource": True,
                   "sseSecondTurn": True, "historyReplay": True, "ragflowSession": True,
                   "documentScopedRetrieval": True,
                   "pathTraversalRejected": True, "shaMismatchRejected": True, "aclOverreachRejected": True,
                   "bounded100MBCheck": large_checked}
        return summary
    finally:
        if not reuse_source:
            staged.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Required FILE_SHARE/v2 HTTP E2E")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "artifacts" / "e2e" / "file-share-v3-v2")
    parser.add_argument("--target-mode", choices=("local", "docker"), default="local")
    parser.add_argument("--callback-mode", choices=("temporary", "existing"), default="temporary")
    parser.add_argument("--resume-external-document-id", default="")
    return parser


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifacts = Artifacts(args.artifact_dir)
    try:
        evidence = run_live(artifacts, target_mode=args.target_mode, callback_mode=args.callback_mode,
                            resume_document=args.resume_external_document_id)
        payload: dict[str, Any] = {"profile": "Integration", "passed": True, "evidence": evidence}
        code = 0
    except LiveEnvironmentError as exc:
        payload = {"profile": "Integration", "passed": False, "outcome": "BLOCKED", "reason": str(exc)}; code = 3
    except LiveAssertionError as exc:
        payload = {"profile": "Integration", "passed": False, "outcome": "test_failure", "reason": str(exc)}; code = 1
    except Exception:
        payload = {"profile": "Integration", "passed": False, "outcome": "runner_failure"}; code = 4
    _write_report(args.report, payload)
    artifacts.write(payload)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
