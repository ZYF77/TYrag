"""Static, offline acceptance checks for the external integration contract."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

import pytest
import yaml
from jsonschema.validators import validator_for


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "v2_contract_expectations.json"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def _expectations() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _load_document(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"required contract file is missing: {path.relative_to(ROOT)}"
    text = path.read_text(encoding="utf-8")
    document = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    assert isinstance(document, dict), f"contract must contain an object: {path.relative_to(ROOT)}"
    return document


def _contract() -> tuple[Path, dict[str, Any]]:
    path = ROOT / _expectations()["contract"]
    return path, _load_document(path)


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS and isinstance(operation, dict):
                yield path, method.lower(), operation


def _pointer(document: Any, fragment: str) -> Any:
    current = document
    if not fragment:
        return current
    assert fragment.startswith("/"), f"unsupported JSON pointer: #{fragment}"
    for raw_part in fragment[1:].split("/"):
        part = unquote(raw_part).replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _resolve_ref(ref: str, base_path: Path) -> tuple[Path, Any]:
    file_part, separator, fragment = ref.partition("#")
    target_path = (base_path.parent / file_part).resolve() if file_part else base_path.resolve()
    contracts_root = (ROOT / "contracts").resolve()
    assert target_path == contracts_root or contracts_root in target_path.parents, (
        f"external contract ref escapes contracts/: {ref}"
    )
    document = _load_document(target_path)
    return target_path, _pointer(document, fragment if separator else "")


def _schema_nodes(
    schema: Any,
    base_path: Path,
    location: str,
    seen_refs: set[tuple[Path, str]] | None = None,
) -> Iterator[tuple[Any, Path, str]]:
    if not isinstance(schema, dict):
        return
    seen_refs = seen_refs if seen_refs is not None else set()
    if "$ref" in schema:
        ref = schema["$ref"]
        marker = (base_path.resolve(), ref)
        if marker in seen_refs:
            return
        seen_refs.add(marker)
        resolved_path, resolved = _resolve_ref(ref, base_path)
        yield from _schema_nodes(resolved, resolved_path, f"{location} -> {ref}", seen_refs)
        return
    yield schema, base_path, location
    for keyword in ("allOf", "anyOf", "oneOf"):
        for index, child in enumerate(schema.get(keyword, [])):
            yield from _schema_nodes(child, base_path, f"{location}.{keyword}[{index}]", seen_refs)
    for name, child in schema.get("properties", {}).items():
        yield from _schema_nodes(child, base_path, f"{location}.{name}", seen_refs)
    for keyword in ("items", "contains", "not", "if", "then", "else"):
        if keyword in schema:
            yield from _schema_nodes(schema[keyword], base_path, f"{location}.{keyword}", seen_refs)


def _request_schemas(
    spec_path: Path, spec: dict[str, Any]
) -> Iterator[tuple[Any, Path, str]]:
    for path, method, operation in _operations(spec):
        request_body = operation.get("requestBody")
        if not isinstance(request_body, dict):
            continue
        if "$ref" in request_body:
            request_path, request_body = _resolve_ref(request_body["$ref"], spec_path)
        else:
            request_path = spec_path
        for media_type, media in request_body.get("content", {}).items():
            if isinstance(media, dict) and "schema" in media:
                yield media["schema"], request_path, f"{method.upper()} {path} ({media_type})"


@pytest.mark.parametrize("path", sorted((ROOT / "contracts").glob("*.y*ml")))
def test_contract_yaml_files_parse(path: Path):
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


@pytest.mark.parametrize("path", sorted((ROOT / "contracts").glob("*.json")))
def test_contract_json_files_parse_and_schemas_are_valid(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    if "$schema" in document:
        validator_for(document).check_schema(document)


def test_v2_contract_identity_and_server_base():
    _, spec = _contract()
    assert str(spec.get("openapi", "")).startswith("3.1."), "v2 contract must use OpenAPI 3.1"
    servers = spec.get("servers", [])
    suffix = _expectations()["serverPathSuffix"]
    assert any(str(server.get("url", "")).rstrip("/").endswith(suffix) for server in servers)


def test_v2_required_operations_are_present():
    _, spec = _contract()
    paths = spec.get("paths", {})
    missing = [
        f"{method.upper()} {path}"
        for path, method in _expectations()["requiredOperations"]
        if not isinstance(paths.get(path), dict) or method not in paths[path]
    ]
    assert not missing, f"v2 contract is missing required operations: {missing}"


def test_v2_operation_ids_are_present_unique_and_statuses_are_valid():
    _, spec = _contract()
    operations = list(_operations(spec))
    assert operations, "v2 contract has no operations"
    operation_ids = [operation.get("operationId") for _, _, operation in operations]
    assert all(isinstance(value, str) and value.strip() for value in operation_ids)
    assert len(operation_ids) == len(set(operation_ids)), "operationId values must be unique"
    allowed = set(_expectations()["allowedStatuses"])
    invalid = [
        f"{method.upper()} {path}: {operation.get('x-status')!r}"
        for path, method, operation in operations
        if operation.get("x-status") not in allowed
    ]
    assert not invalid, f"missing or invalid x-status: {invalid}"


def test_v2_wire_identifiers_do_not_expose_ragflow_internals():
    spec_path, spec = _contract()
    forbidden = {
        re.sub(r"[^a-z0-9]", "", value.lower())
        for value in _expectations()["forbiddenIdentifiers"]
    }
    violations: list[str] = []

    seen_refs: set[tuple[Path, str]] = set()

    def visit(value: Any, location: str, base_path: Path) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                marker = (base_path.resolve(), ref)
                if marker not in seen_refs:
                    seen_refs.add(marker)
                    resolved_path, resolved = _resolve_ref(ref, base_path)
                    visit(resolved, f"{location} -> {ref}", resolved_path)
            properties = value.get("properties", {})
            if isinstance(properties, dict):
                for name in properties:
                    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
                    if normalized in forbidden:
                        violations.append(f"{location}.properties.{name}")
            if value.get("in") in {"path", "query", "header", "cookie"}:
                name = value.get("name")
                if isinstance(name, str):
                    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
                    if normalized in forbidden:
                        violations.append(f"{location}.name={name}")
            for key, child in value.items():
                visit(child, f"{location}.{key}", base_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]", base_path)

    visit(spec, "openapi", spec_path)
    assert not violations, f"v2 wire contract exposes RAGFlow internal identifiers: {violations}"


def test_v2_json_request_objects_are_strict():
    spec_path, spec = _contract()
    violations: list[str] = []
    request_count = 0
    for schema, base_path, location in _request_schemas(spec_path, spec):
        request_count += 1
        for node, _, node_location in _schema_nodes(schema, base_path, location):
            is_object = node.get("type") == "object" or "properties" in node
            if is_object and node.get("additionalProperties") is not False:
                violations.append(node_location)
    assert request_count, "v2 contract has no JSON request schemas"
    assert not violations, (
        "every object in a v2 request schema must set additionalProperties: false; "
        f"violations: {violations}"
    )


def test_p0_operations_are_implemented_and_p1_attachment_is_planned():
    _, spec = _contract()
    paths = spec["paths"]
    for path, method in _expectations()["requiredOperations"]:
        assert paths[path][method]["x-status"] == "implemented", f"{method} {path}"
    assert (
        paths["/conversations/{conversationId}/attachments"]["post"]["x-status"]
        == "planned"
    )


def test_document_idempotency_and_metadata_boundary_are_frozen():
    _, spec = _contract()
    schemas = spec["components"]["schemas"]
    command = schemas["DocumentCommand"]
    assert command["properties"]["eventType"]["enum"] == ["upsert", "reindex"]
    assert {"eventId", "tenantId", "sourceSystem", "externalDocumentId", "sourceVersionId"} <= set(
        command["required"]
    )
    metadata = schemas["DocumentMetadata"]
    assert {"tenant_id", "source_system", "external_document_id", "equipment_id"} <= set(
        metadata["required"]
    )
    assert "tenant_id" not in command["properties"]
    operation = schemas["DocumentOperation"]
    assert "deduplicated" in operation["required"]
    assert "202" in spec["paths"]["/documents"]["post"]["responses"]
    freeze = (ROOT / "contracts" / "external-integration-contract-freeze-v2.md").read_text(
        encoding="utf-8"
    )
    assert "EVENT_ID_CONFLICT" in freeze
    assert "DOCUMENT_VERSION_CONFLICT" in freeze


def test_hmac_trust_boundary_and_binding_pair_are_frozen():
    _, spec = _contract()
    components = spec["components"]
    scheme = components["securitySchemes"]["HmacSignature"]
    assert scheme["name"] == "X-TY-Signature"
    assert components["parameters"]["TimestampHeader"]["name"] == "X-TY-Timestamp"
    assert components["parameters"]["KeyIdHeader"]["name"] == "X-TY-Key-Id"
    identity = components["schemas"]["CredentialIdentity"]
    assert "allowedBindings" in identity["required"]
    binding = components["schemas"]["CredentialBinding"]
    assert set(binding["required"]) == {"tenantId", "sourceSystem"}
    document_security = spec["paths"]["/documents"]["post"]["security"]
    assert document_security == [{"HmacSignature": []}]


def test_message_oneof_prompt_exclusion_and_suggestion_version_are_frozen():
    _, spec = _contract()
    schemas = spec["components"]["schemas"]
    assert len(schemas["CreateMessageRequest"]["oneOf"]) == 2
    forbidden = {"systemPrompt", "hiddenPrompt", "promptOverride", "tools", "toolDefinitions"}
    for name in ("QuestionMessageRequest", "SuggestionMessageRequest"):
        assert not (forbidden & set(schemas[name]["properties"]))
    assert set(schemas["Suggestion"]["required"]) >= {
        "suggestionId",
        "label",
        "displayPrompt",
        "contextVersion",
    }
    assert "contextVersion" in schemas["SuggestionMessageRequest"]["required"]


def test_p1_callback_and_attachment_invariants_are_explicit():
    _, spec = _contract()
    callback = spec["components"]["schemas"]["CallbackEvent"]
    assert callback["x-status"] == "planned"
    assert set(callback["required"]) == {
        "deliveryId",
        "eventType",
        "originatingEventId",
        "externalDocumentId",
        "sourceVersionId",
        "status",
        "timestamp",
        "payloadVersion",
    }
    attachment = spec["paths"]["/conversations/{conversationId}/attachments"]["post"]
    assert "indexPolicy is always never" in attachment["description"]


def test_error_code_http_statuses_match_the_v2_freeze():
    errors = _load_document(ROOT / "contracts" / "error-codes.yaml")["errors"]
    by_code = {item["code"]: item["http_status"] for item in errors}
    assert by_code["DOCUMENT_EVENT_DUPLICATE"] == 202
    for code in (
        "EVENT_ID_CONFLICT",
        "DOCUMENT_VERSION_CONFLICT",
        "CONVERSATION_CONTEXT_CONFLICT",
        "CLIENT_MESSAGE_ID_CONFLICT",
        "SUGGESTION_STALE",
    ):
        assert by_code[code] == 409


def test_freeze_has_no_p0_open_question_and_ends_with_the_verdict():
    lines = [
        line.strip()
        for line in (ROOT / "contracts" / "external-integration-contract-freeze-v2.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert lines[-1] == "CONTRACT FROZEN"
    assert "## 18. Remaining Open Questions" in lines
    assert any("无影响 P0 接口实现的契约问题" in line for line in lines)
