"""Offline validation for the device Postman collection and environment template."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


_VARIABLE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_.-]*)\s*\}\}")
_SENSITIVE_KEYS = {"hmacsecret", "userjwt", "jwt", "token", "password", "secret"}
_FORBIDDEN_ROUTE_PARTS = (
    "/enterprise/api/v1",
    "/enterprise/api/demo",
    "/enterprise/api/s3",
)

_ENVIRONMENT_SCHEMA = {
    "type": "object",
    "required": ["info", "values"],
    "properties": {
        "info": {"type": "object"},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "value", "type", "enabled"],
                "properties": {
                    "key": {"type": "string", "minLength": 1},
                    "value": {},
                    "type": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}

_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["method", "header", "auth", "body", "url"],
    "properties": {
        "method": {"type": "string", "enum": ["GET", "POST"]},
        "header": {"type": "array"},
        "auth": {"type": "object"},
        "body": {"type": "object"},
        "url": {},
    },
    "additionalProperties": True,
}


def _json_errors(schema: dict[str, Any], value: Any, prefix: str) -> list[str]:
    return [f"{prefix}: {error.message}" for error in Draft202012Validator(schema).iter_errors(value)]


def _request_url(request: dict[str, Any]) -> str:
    value = request.get("url", "")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("raw")
        if isinstance(raw, str):
            return raw
        path = value.get("path")
        if isinstance(path, list):
            return "/" + "/".join(str(part) for part in path)
    return ""


def _iter_items(items: list[dict[str, Any]], parents: tuple[dict[str, Any], ...] = ()):
    for item in items:
        if "request" in item:
            yield item, parents
        children = item.get("item", [])
        if isinstance(children, list):
            yield from _iter_items(children, parents + (item,))


def _event_scripts(item: dict[str, Any], parents: tuple[dict[str, Any], ...]) -> tuple[str, str]:
    pre_request: list[str] = []
    tests: list[str] = []
    for container in parents + (item,):
        for event in container.get("event", []) or []:
            if not isinstance(event, dict):
                continue
            listen = event.get("listen")
            script = event.get("script", {})
            exec_lines = script.get("exec", []) if isinstance(script, dict) else []
            source = "\n".join(str(line) for line in exec_lines)
            if listen == "prerequest":
                pre_request.append(source)
            elif listen == "test":
                tests.append(source)
    return "\n".join(pre_request), "\n".join(tests)


def _declared_variables(environment: dict[str, Any], collection: dict[str, Any]) -> set[str]:
    names = {
        str(value.get("key"))
        for value in environment.get("values", [])
        if isinstance(value, dict) and value.get("key")
    }
    names.update(
        str(value.get("key"))
        for value in collection.get("variable", [])
        if isinstance(value, dict) and value.get("key")
    )
    return names


def validate_artifacts(collection_path: Path, environment_path: Path) -> list[str]:
    """Return all offline validation errors without making network calls."""

    errors: list[str] = []
    try:
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"collection cannot be read as JSON: {exc}"]
    try:
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"environment cannot be read as JSON: {exc}"]

    errors.extend(_json_errors(_ENVIRONMENT_SCHEMA, environment, "environment schema"))
    if collection.get("info", {}).get("schema") != "https://schema.getpostman.com/json/collection/v2.1.0/collection.json":
        errors.append("collection schema must be Postman v2.1")
    if not isinstance(collection.get("item"), list) or not collection["item"]:
        errors.append("collection must contain request items")

    values = {
        str(value.get("key")): value.get("value")
        for value in environment.get("values", [])
        if isinstance(value, dict) and value.get("key")
    }
    for key in ("hmacSecret", "userJwt"):
        if key not in values:
            errors.append(f"environment must declare {key}")
        elif values[key] not in ("", None):
            errors.append(f"environment template must leave {key} empty")
    for key, value in values.items():
        normalized = key.lower().replace("_", "")
        if normalized in _SENSITIVE_KEYS or normalized.endswith("secret"):
            if value not in ("", None):
                errors.append(f"sensitive environment value is not empty: {key}")

    declared = _declared_variables(environment, collection)
    seen_requests = 0
    for item, parents in _iter_items(collection.get("item", [])):
        seen_requests += 1
        name = str(item.get("name", "<unnamed request>"))
        request = item.get("request")
        if not isinstance(request, dict):
            errors.append(f"{name}: request must be an object")
            continue
        errors.extend(_json_errors(_REQUEST_SCHEMA, request, f"{name} request schema"))
        url = _request_url(request)
        if not url:
            errors.append(f"{name}: URL is required")
        lower_url = url.lower()
        for forbidden in _FORBIDDEN_ROUTE_PARTS:
            if forbidden in lower_url:
                errors.append(f"{name}: forbidden route {forbidden}")
        for variable in _VARIABLE.findall(url + json.dumps(request, ensure_ascii=False)):
            if variable not in declared:
                errors.append(f"{name}: undeclared variable {variable}")

        pre_request, tests = _event_scripts(item, parents)
        if "pm.test" not in tests or "pm.response" not in tests:
            errors.append(f"{name}: response tests are required")
        auth = request.get("auth", {})
        auth_type = auth.get("type") if isinstance(auth, dict) else None
        if auth_type not in {"noauth", "bearer"}:
            errors.append(f"{name}: explicit noauth or bearer authorization is required")
        if (
            auth_type == "bearer"
            and "{{userJwt}}" not in json.dumps(auth, ensure_ascii=False)
            and name != "JWT v2 · invalid JWT"
        ):
            errors.append(f"{name}: bearer authorization must use userJwt")
        if "FILE_SHARE v3" in " / ".join(str(parent.get("name", "")) for parent in parents):
            if "pm.vault" not in pre_request or "X-TY-Signature" not in pre_request:
                errors.append(f"{name}: FILE_SHARE requests must inherit the HMAC pre-request script")
        if name == "FILE_SHARE v3 · poll diagnostic status" and url.strip() != "{{baseUrl}}{{statusUrl}}":
            errors.append(f"{name}: diagnostic polling URL must prefix statusUrl with baseUrl")

    if seen_requests == 0:
        errors.append("collection contains no leaf requests")
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate secret-free Postman artifacts")
    parser.add_argument("collection", type=Path)
    parser.add_argument("environment", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    errors = validate_artifacts(args.collection, args.environment)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Postman artifacts are offline-valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
