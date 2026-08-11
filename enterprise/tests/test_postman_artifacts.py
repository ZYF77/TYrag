"""Offline checks for the secret-free device integration Postman artifacts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt

from enterprise.gateway.auth.service_auth import canonical_request, sign_request
from enterprise.scripts.generate_postman_local_environment import _parser, build_user_jwt
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
