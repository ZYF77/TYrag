"""Development-only HTTP stub for the Asset Registry boundary.

This process is intentionally not part of the formal Integration acceptance
path. It exists to exercise the frozen Gateway adapter while the real device
management Asset Registry is still being provisioned.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


AssetRecord = dict[str, str | None]

DEFAULT_RECORDS: tuple[AssetRecord, ...] = (
    {
        "tenantId": "wp04e2e",
        "equipmentId": "EQ-E2E-001",
        "fixedAssetNo": "FA-Doc1",
        "assetId": "FA-Doc1",
        "registryVersion": "dev-stub-v1",
    },
    {
        "tenantId": "wp04e2e",
        "equipmentId": "EQ-E2E-002",
        "fixedAssetNo": "FA-Doc2",
        "assetId": "FA-Doc2",
        "registryVersion": "dev-stub-v1",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_records(items: list[AssetRecord]) -> list[AssetRecord]:
    records: list[AssetRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each Asset Registry stub record must be an object")

        tenant_id = item.get("tenantId")
        equipment_id = item.get("equipmentId")
        fixed_asset_no = item.get("fixedAssetNo")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("Each stub record requires a non-empty tenantId")
        if not isinstance(equipment_id, str) or not equipment_id.strip():
            raise ValueError("Each stub record requires a non-empty equipmentId")
        if not isinstance(fixed_asset_no, str) or not fixed_asset_no.strip():
            raise ValueError("Each stub record requires a non-empty fixedAssetNo")

        record: AssetRecord = {
            "tenantId": tenant_id,
            "equipmentId": equipment_id,
            "fixedAssetNo": fixed_asset_no,
            "assetId": item.get("assetId"),
            "registryVersion": item.get("registryVersion") or "dev-stub-v1",
        }
        for field in ("assetId", "registryVersion"):
            value = record[field]
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"Stub field {field} must be a non-empty string or null")
        records.append(record)

    equipment_to_fixed: dict[tuple[str, str], str] = {}
    fixed_to_equipment: dict[tuple[str, str], str] = {}
    asset_to_identity: dict[tuple[str, str], tuple[str, str]] = {}
    seen_pairs: set[tuple[str, str, str]] = set()
    for record in records:
        tenant_id = record["tenantId"]
        equipment_id = record["equipmentId"]
        fixed_asset_no = record["fixedAssetNo"]
        pair_key = (tenant_id, equipment_id, fixed_asset_no)
        if pair_key in seen_pairs:
            raise ValueError(
                "Asset Registry stub fixture contains a duplicate equipmentId/fixedAssetNo pair"
            )
        seen_pairs.add(pair_key)

        equipment_key = (tenant_id, equipment_id)
        if equipment_key in equipment_to_fixed:
            raise ValueError(
                "Asset Registry stub fixture violates the equipmentId/fixedAssetNo one-to-one mapping"
            )
        equipment_to_fixed[equipment_key] = fixed_asset_no

        fixed_key = (tenant_id, fixed_asset_no)
        if fixed_key in fixed_to_equipment:
            raise ValueError(
                "Asset Registry stub fixture violates the equipmentId/fixedAssetNo one-to-one mapping"
            )
        fixed_to_equipment[fixed_key] = equipment_id

        asset_id = record["assetId"]
        if asset_id is not None:
            asset_key = (tenant_id, asset_id)
            if asset_key in asset_to_identity:
                raise ValueError(
                    "Asset Registry stub fixture maps one assetId to multiple identities"
                )
            asset_to_identity[asset_key] = (equipment_id, fixed_asset_no)
    return records


def _load_records(data_file: str | None = None) -> list[AssetRecord]:
    path_value = data_file or os.environ.get("ENTERPRISE_ASSET_REGISTRY_STUB_DATA", "")
    if not path_value.strip():
        return _normalise_records([dict(record) for record in DEFAULT_RECORDS])

    payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("assets")
    if not isinstance(payload, list):
        raise ValueError("Asset Registry stub data must be a JSON array or {\"assets\": [...]}")
    return _normalise_records(payload)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"code": code, "message": message},
        status_code=status_code,
    )


def _resolve(
    records: list[AssetRecord],
    *,
    tenant_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    asset_id: str | None,
) -> tuple[int, dict[str, Any]]:
    supplied = {
        "equipmentId": equipment_id,
        "fixedAssetNo": fixed_asset_no,
        "assetId": asset_id,
    }
    if any(
        value is not None and (not isinstance(value, str) or not value.strip())
        for value in supplied.values()
    ):
        return 422, {
            "code": "ASSET_IDENTIFIER_REQUIRED",
            "message": "Asset identifiers must be non-empty",
        }
    if not any(supplied.values()):
        return 422, {
            "code": "ASSET_IDENTIFIER_REQUIRED",
            "message": "At least one asset identifier is required",
        }

    tenant_records = [record for record in records if record["tenantId"] == tenant_id]
    match_sets = [
        {
            index
            for index, record in enumerate(tenant_records)
            if record[field] == value
        }
        for field, value in supplied.items()
        if value is not None
    ]
    if not match_sets or all(not matches for matches in match_sets):
        return 404, {
            "code": "ASSET_NOT_FOUND",
            "message": "Asset identifier was not found",
        }
    if any(not matches for matches in match_sets):
        return 409, {
            "code": "ASSET_IDENTIFIER_CONFLICT",
            "message": "Asset identifiers do not agree",
        }

    candidate_indexes = set.intersection(*match_sets)
    if not candidate_indexes:
        return 409, {
            "code": "ASSET_IDENTIFIER_CONFLICT",
            "message": "Asset identifiers resolve to multiple registry identities",
        }
    if len(candidate_indexes) != 1:
        return 409, {
            "code": "ASSET_IDENTIFIER_CONFLICT",
            "message": "Asset identifiers resolve to multiple registry identities",
        }

    record = tenant_records[next(iter(candidate_indexes))]
    return 200, {
        "tenantId": tenant_id,
        "equipmentId": record["equipmentId"],
        "fixedAssetNo": record["fixedAssetNo"],
        "assetId": record["assetId"],
        "registryVersion": record["registryVersion"],
        "resolvedAt": _now(),
    }


def create_app(
    records: list[AssetRecord] | None = None,
    *,
    token: str | None = None,
) -> FastAPI:
    known_records = _load_records() if records is None else _normalise_records(records)
    expected_token = (
        os.environ.get("ENTERPRISE_ASSET_REGISTRY_TOKEN", "").strip()
        if token is None
        else token.strip()
    )
    app = FastAPI(title="TYRAG Development Asset Registry Stub")

    @app.middleware("http")
    async def mark_dev_stub(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-TYRAG-ASSET-REGISTRY-IMPLEMENTATION"] = "dev-stub"
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "implementation": "dev-stub"}

    @app.get("/v1/assets/resolve")
    async def resolve(request: Request) -> JSONResponse:
        if expected_token and request.headers.get("authorization") != f"Bearer {expected_token}":
            return _error(401, "ASSET_REGISTRY_UNAUTHORIZED", "Bearer token is required")

        query = request.query_params
        tenant_id = query.get("tenantId", "").strip()
        if not tenant_id:
            return _error(422, "TENANT_REQUIRED", "tenantId is required")
        status_code, payload = _resolve(
            known_records,
            tenant_id=tenant_id,
            equipment_id=query.get("equipmentId"),
            fixed_asset_no=query.get("fixedAssetNo"),
            asset_id=query.get("assetId"),
        )
        return JSONResponse(payload, status_code=status_code)

    return app


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the dev-only Asset Registry stub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9390)
    parser.add_argument("--data-file", help="JSON array or {\"assets\": [...]} file")
    args = parser.parse_args()
    selected_app = create_app(_load_records(args.data_file))
    uvicorn.run(selected_app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
