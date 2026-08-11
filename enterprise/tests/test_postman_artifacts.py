"""Offline checks for the secret-free device integration Postman artifacts."""

from __future__ import annotations

import json
import time
import zlib
from pathlib import Path

import jwt
import pytest

from enterprise.gateway.auth.service_auth import canonical_request, sign_request
from enterprise.scripts.generate_postman_local_environment import (
    _count_pdf_pages_from_bytes,
    _parser,
    build_user_jwt,
    main as generate_local_environment,
)
from enterprise.scripts.validate_postman_artifacts import validate_artifacts


REPO_ROOT = Path(__file__).resolve().parents[2]
POSTMAN_ROOT = REPO_ROOT / "enterprise" / "postman"
COLLECTION_PATH = POSTMAN_ROOT / "tyrag-device-integration.postman_collection.json"
ENVIRONMENT_PATH = POSTMAN_ROOT / "tyrag-local.postman_environment.template.json"


def test_postman_collection_and_environment_are_offline_valid():
    errors = validate_artifacts(COLLECTION_PATH, ENVIRONMENT_PATH)
    assert errors == []


def test_collection_has_required_p0_examples_and_only_frozen_routes():
    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(collection, ensure_ascii=False)
    names = set()

    def visit(items):
        for item in items:
            if "request" in item:
                names.add(item["name"])
            visit(item.get("item", []))

    visit(collection["item"])
    assert {
        "FILE_SHARE v3 · register",
        "FILE_SHARE v3 · duplicate register",
        "FILE_SHARE v3 · poll response statusUrl",
        "FILE_SHARE v3 · invalid HMAC",
        "FILE_SHARE v3 · missing source registration",
        "FILE_SHARE v3 · missing document status error",
        "JWT v2 · create session",
        "JWT v2 · question round 1",
        "JWT v2 · question round 2",
        "JWT v2 · history",
        "JWT v2 · citation detail",
        "JWT v2 · citation source",
        "JWT v2 · invalid JWT",
    } <= names
    assert "/enterprise/api/v3/" in serialized
    assert "/enterprise/api/v2/" in serialized
    assert "/enterprise/api/v1" not in serialized
    assert "/enterprise/api/demo" not in serialized
    assert "/enterprise/api/s3" not in serialized.lower()
    assert "pm.vault" in serialized
    assert "statusUrl" in serialized


def test_local_defaults_and_polling_are_runner_safe():
    environment = json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))
    values = {entry["key"]: entry["value"] for entry in environment["values"]}
    assert values["baseUrl"] == "http://127.0.0.1:5188"
    assert values["pollAttempt"] == "0"
    assert values["maxPollAttempts"] == "120"

    args = _parser().parse_args(
        ["--file", "manual.pdf", "--output", "device.local.postman_environment.json"]
    )
    assert args.base_url == "http://127.0.0.1:5188"
    assert args.storage_root_id == "device-share"
    assert "设备" in args.question_one
    assert "设备" in args.question_two

    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    poll = next(
        item
        for folder in collection["item"]
        for item in folder.get("item", [])
        if item.get("name") == "FILE_SHARE v3 · poll response statusUrl"
    )
    assert poll["request"]["url"] == "{{baseUrl}}{{statusUrl}}"
    test_script = "\n".join(
        line
        for event in poll["event"]
        if event["listen"] == "test"
        for line in event["script"]["exec"]
    )
    assert "payload.retrievable" in test_script
    assert "payload.pipelineStatus" in test_script
    assert "payload.parseCompleted" in test_script
    assert "payload.indexCompleted" in test_script
    assert "payload.qualityStatus" in test_script
    assert "payload.errorCode" in test_script
    assert "pollAttempt" in test_script
    assert "maxPollAttempts" in test_script
    assert "pm.execution.setNextRequest" in test_script

    runbook = (REPO_ROOT / "docs" / "integration" / "device-postman-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "--delay-request 2000" in runbook
    assert "2000ms" in runbook
    assert "Collection Runner" in runbook
    assert "普通 Send" in runbook


def test_collection_uses_real_page_count_and_strict_query_assertions():
    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    raw_bodies = [
        item["request"]["body"]["raw"]
        for folder in collection["item"]
        for item in folder.get("item", [])
        if item.get("request", {}).get("body", {}).get("mode") == "raw"
    ]
    assert any('"page_count":{{pageCount}}' in body for body in raw_bodies)
    assert all('"page_count":1' not in body for body in raw_bodies)
    assert any('"question":"{{questionOne}}"' in body for body in raw_bodies)
    assert any('"question":"{{questionTwo}}"' in body for body in raw_bodies)

    for name in ("JWT v2 · question round 1", "JWT v2 · question round 2"):
        item = next(
            item
            for folder in collection["item"]
            for item in folder.get("item", [])
            if item.get("name") == name
        )
        test_script = "\n".join(
            line
            for event in item["event"]
            if event["listen"] == "test"
            for line in event["script"]["exec"]
        )
        assert "payload.status).to.eql('completed')" in test_script
        assert "payload.citations" in test_script
        assert "payload.answer" in test_script
        assert "i don't have enough information" in test_script
        assert "未找到可靠依据" in test_script


def test_standard_library_pdf_page_count_handles_plain_and_object_stream_pages():
    plain = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Pages /Count 2 >> endobj\n"
        b"2 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 1 0 R >> endobj\n%%EOF"
    )
    assert _count_pdf_pages_from_bytes(plain) == 2

    object_stream = (
        b"10 0 << /Type /Pages /Count 3 >> "
        b"11 0 << /Type /Page >> 12 0 << /Type /Page >> "
        b"13 0 << /Type /Page >>"
    )
    compressed = zlib.compress(object_stream)
    flate = (
        b"%PDF-1.5\n4 0 obj << /Type /ObjStm /Filter /FlateDecode "
        + f"/Length {len(compressed)}".encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n%%EOF"
    )
    assert _count_pdf_pages_from_bytes(flate) == 3

    with pytest.raises(ValueError, match="page count"):
        _count_pdf_pages_from_bytes(b"%PDF-1.7\n%%EOF")


def test_generator_writes_page_count_questions_and_unified_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pdf_path = tmp_path / "设备单机调试记录.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    output = tmp_path / "device.local.postman_environment.json"
    monkeypatch.setenv("TYRAG_JWT_SHARED_SECRET", "j" * 48)
    monkeypatch.setenv("TYRAG_HMAC_SECRET", "h" * 48)

    assert generate_local_environment(
        [
            "--file",
            str(pdf_path),
            "--output",
            str(output),
            "--page-count",
            "7",
            "--question-one",
            "第一轮设备问题",
            "--question-two",
            "第二轮设备问题",
        ]
    ) == 0
    environment = json.loads(output.read_text(encoding="utf-8"))
    assert environment["_postman_variable_scope"] == "environment"
    assert "info" not in environment
    values = {entry["key"]: entry["value"] for entry in environment["values"]}
    assert values["pageCount"] == "7"
    assert values["storageRootId"] == "device-share"
    assert values["questionOne"] == "第一轮设备问题"
    assert values["questionTwo"] == "第二轮设备问题"


def test_collection_signer_resolves_dynamic_status_url_before_signing():
    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(collection, ensure_ascii=False)
    assert "pm.variables.replaceIn(pm.request.url.toString())" in serialized
    assert "var resolvedTarget = resolvedPathAndQuery()" in serialized


def test_hmac_fixed_vector_matches_python_producer():
    secret = "".join(chr(65 + (index % 26)) for index in range(48))
    body = b'{"a":"two words","z":"%"}'
    query = "b=two%20words&a=last&a=first&empty="
    canonical = canonical_request(
        timestamp="1700000000",
        method="get",
        path="/enterprise/api/v3/documents/MISSING",
        query=query,
        body=body,
    ).decode("utf-8")
    assert canonical.splitlines() == [
        "v1",
        "1700000000",
        "GET",
        "/enterprise/api/v3/documents/MISSING?a=first&a=last&b=two%20words&empty=",
        "fcc1e0e0a9d9ad51515bc588c7ddc973e4bd55a723e10a380abae872bc9d6d7a",
    ]
    assert sign_request(
        secret=secret,
        timestamp="1700000000",
        method="get",
        path="/enterprise/api/v3/documents/MISSING",
        query=query,
        body=body,
    ) == "v1=937605b88f03f06bc138e002e09b70ef0471a1af64c8fdce35b46323ea18621d"

    script = (POSTMAN_ROOT / "hmac-file-share-v3-pre-request.js").read_text(
        encoding="utf-8"
    )
    assert "canonicalPathQuery" in script
    assert "X-TY-Timestamp" in script
    assert "X-TY-Key-Id" in script
    assert "X-TY-Signature" in script
    assert "CryptoJS.SHA256" in script
    assert "pm.vault" in script


def test_local_identity_tool_uses_existing_gateway_hs256_claims():
    secret = "".join(chr(97 + (index % 26)) for index in range(48))
    token = build_user_jwt(
        secret=secret,
        issuer="https://auth.example.test",
        audience="tyrag-gateway-test",
        tenant="device-tenant",
        user="device-user",
        now=int(time.time()),
        lifetime_seconds=3600,
    )
    claims = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer="https://auth.example.test",
        audience="tyrag-gateway-test",
    )
    assert claims["sub"] == "device-user"
    assert claims["tenant"] == "device-tenant"
    assert claims["roles"] == ["end_user"]
    assert claims["groups"] == ["maintenance"]
    assert claims["iss"] == "https://auth.example.test"
