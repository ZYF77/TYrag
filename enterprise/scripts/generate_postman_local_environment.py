"""Create an untracked Postman/Newman environment for a local device test.

The script intentionally reads secrets only from process environment variables
and writes them only to an explicitly local, ignored Postman environment file.
It generates the same HS256 claim shape used by the Gateway tests; it does not
introduce a second authentication protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import jwt


DEFAULT_ISSUER = "https://auth.example.test"
DEFAULT_AUDIENCE = "tyrag-gateway-test"


def build_user_jwt(
    *,
    secret: str,
    issuer: str,
    audience: str,
    tenant: str,
    user: str,
    now: int,
    lifetime_seconds: int,
    roles: tuple[str, ...] = ("end_user",),
    groups: tuple[str, ...] = ("maintenance",),
) -> str:
    """Build an HS256 JWT with the Gateway's existing test claim mapping."""

    if not secret:
        raise ValueError("JWT signing secret is required")
    if lifetime_seconds <= 0:
        raise ValueError("JWT lifetime must be positive")
    claims = {
        "sub": user,
        "tenant": tenant,
        "name": user,
        "department": ["maintenance"],
        "roles": list(roles),
        "groups": list(groups),
        "security_level": 2,
        "iat": now - 60,
        "exp": now + lifetime_seconds,
        "iss": issuer,
        "aud": audience,
    }
    return str(jwt.encode(claims, secret, algorithm="HS256"))


def _value(key: str, value: Any, *, enabled: bool = True) -> dict[str, Any]:
    return {"key": key, "value": str(value) if enabled else "", "type": "default", "enabled": True}


def build_environment(
    *,
    base_url: str,
    tenant: str,
    source_system: str,
    key_id: str,
    hmac_secret: str,
    user_jwt: str,
    equipment_id: str,
    fixed_asset_no: str,
    fault_code: str,
    storage_root_id: str,
    relative_path: str,
    file_name: str,
    file_sha256: str,
    file_size: int,
    source_version_id: str,
    document_type: str,
    department_id: str,
    security_level: int,
    allow_group_id: str,
) -> dict[str, Any]:
    """Return a Postman v2.1 environment, including the local-only secrets."""

    values = [
        _value("baseUrl", base_url),
        _value("tenantId", tenant),
        _value("sourceSystem", source_system),
        _value("keyId", key_id),
        _value("hmacSecret", hmac_secret),
        _value("userJwt", user_jwt),
        _value("invalidJwt", ""),
        _value("equipmentId", equipment_id),
        _value("fixedAssetNo", fixed_asset_no),
        _value("faultCode", fault_code),
        _value("storageRootId", storage_root_id),
        _value("relativePath", relative_path),
        _value("fileName", file_name),
        _value("fileSha256", file_sha256),
        _value("fileSize", file_size),
        _value("externalDocumentId", "DEVICE-MANUAL-001"),
        _value("eventId", "device-postman-event-001"),
        _value("invalidEventId", "device-postman-invalid-hmac-001"),
        _value("missingEventId", "device-postman-missing-source-001"),
        _value("missingExternalDocumentId", "DEVICE-MISSING-SOURCE-001"),
        _value("missingRelativePath", "missing/manual.pdf"),
        _value("sourceVersionId", source_version_id),
        _value("documentType", document_type),
        _value("departmentId", department_id),
        _value("securityLevel", security_level),
        _value("allowGroupId", allow_group_id),
        _value("hmacTimestamp", ""),
        _value("statusUrl", ""),
        _value("missingStatusUrl", ""),
        _value("pollAttempt", "0"),
        _value("maxPollAttempts", "120"),
        _value("conversationId", ""),
        _value("citationId", ""),
        _value("messageIdOne", "device-question-001"),
        _value("messageIdTwo", "device-question-002"),
        _value("queryCursor", ""),
    ]
    return {
        "id": "tyrag-device-local",
        "name": "TYRAG device local (do not export)",
        "info": {
            "name": "TYRAG device local (do not export)",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "values": values,
        "_postman_variable_scope": "environment",
        "_postman_exported_at": "",
        "_postman_exported_using": "",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an ignored local Postman/Newman environment"
    )
    parser.add_argument("--file", required=True, type=Path, help="local FILE_SHARE PDF")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="ignored path ending in .local.postman_environment.json",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:5188")
    parser.add_argument("--tenant", default="device-tenant")
    parser.add_argument("--user", default="device-user")
    parser.add_argument("--source-system", default="EAM")
    parser.add_argument("--key-id", default="device-local-key")
    parser.add_argument("--storage-root-id", default="device-file-share")
    parser.add_argument("--relative-path")
    parser.add_argument("--equipment-id", default="EQ-LOCAL-001")
    parser.add_argument("--fixed-asset-no", default="FA-LOCAL-001")
    parser.add_argument("--fault-code", default="FAULT-LOCAL")
    parser.add_argument("--source-version-id", default="local-v3-001")
    parser.add_argument("--document-type", default="PRODUCT_MANUAL")
    parser.add_argument("--department-id", default="maintenance")
    parser.add_argument("--security-level", type=int, default=2)
    parser.add_argument("--allow-group-id", default="maintenance")
    parser.add_argument("--issuer", default=DEFAULT_ISSUER)
    parser.add_argument("--audience", default=DEFAULT_AUDIENCE)
    parser.add_argument("--jwt-secret-env", default="TYRAG_JWT_SHARED_SECRET")
    parser.add_argument("--hmac-secret-env", default="TYRAG_HMAC_SECRET")
    parser.add_argument("--lifetime-seconds", type=int, default=3600)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.output.name.endswith(".local.postman_environment.json"):
        raise SystemExit("--output must end in .local.postman_environment.json")
    if not args.file.is_file():
        raise SystemExit("--file must name an existing local file")
    jwt_secret = os.environ.get(args.jwt_secret_env, "")
    hmac_secret = os.environ.get(args.hmac_secret_env, "")
    if not jwt_secret or not hmac_secret:
        raise SystemExit(
            f"set {args.jwt_secret_env} and {args.hmac_secret_env} in the process environment"
        )

    content = args.file.read_bytes()
    relative_path = args.relative_path or args.file.name
    token = build_user_jwt(
        secret=jwt_secret,
        issuer=args.issuer,
        audience=args.audience,
        tenant=args.tenant,
        user=args.user,
        now=int(time.time()),
        lifetime_seconds=args.lifetime_seconds,
    )
    environment = build_environment(
        base_url=args.base_url,
        tenant=args.tenant,
        source_system=args.source_system,
        key_id=args.key_id,
        hmac_secret=hmac_secret,
        user_jwt=token,
        equipment_id=args.equipment_id,
        fixed_asset_no=args.fixed_asset_no,
        fault_code=args.fault_code,
        storage_root_id=args.storage_root_id,
        relative_path=relative_path,
        file_name=args.file.name,
        file_sha256=hashlib.sha256(content).hexdigest(),
        file_size=len(content),
        source_version_id=args.source_version_id,
        document_type=args.document_type,
        department_id=args.department_id,
        security_level=args.security_level,
        allow_group_id=args.allow_group_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(environment, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote local Postman environment: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
