"""Read-only Asset Registry boundary used by the external v2 contract.

The document map is deliberately not an asset registry.  Production callers
must provide an adapter through the gateway integration boundary; the SQLite
adapter is only an explicit test/development fixture so the offline contract
suite can exercise the same resolution semantics.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx


ASSET_REGISTRY_TTL_SECONDS = 300


class AssetRegistryError(RuntimeError):
    """Base error for the read-only registry boundary."""

    code = "ASSET_REGISTRY_UNAVAILABLE"


class AssetRegistryUnavailable(AssetRegistryError):
    pass


class AssetRegistryConflict(AssetRegistryError):
    code = "CONVERSATION_CONTEXT_CONFLICT"


class AssetRegistryInvalid(AssetRegistryError):
    code = "CONVERSATION_CONTEXT_INVALID"


@dataclass(frozen=True)
class ResolvedAsset:
    tenant_id: str
    equipment_id: str
    fixed_asset_no: str | None
    asset_id: str | None
    registry_version: str | None
    resolved_at: str


class AssetRegistryAdapter(Protocol):
    async def resolve(
        self,
        *,
        tenant_id: str,
        equipment_id: str | None = None,
        fixed_asset_no: str | None = None,
        asset_id: str | None = None,
    ) -> ResolvedAsset | None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteAssetRegistryAdapter:
    """Explicit offline fixture adapter backed by the gateway test DB."""

    def __init__(self, db):
        self.db = db

    async def resolve(
        self,
        *,
        tenant_id: str,
        equipment_id: str | None = None,
        fixed_asset_no: str | None = None,
        asset_id: str | None = None,
    ) -> ResolvedAsset | None:
        identifiers = [
            value
            for value in (equipment_id, fixed_asset_no, asset_id)
            if value
        ]
        if not identifiers:
            return None
        clauses = " OR ".join(
            "equipment_id=? OR fixed_asset_no=? OR asset_id=?"
            for _ in identifiers
        )
        params: list[object] = []
        for value in identifiers:
            params.extend((value, value, value))
        async with self.db.execute(
            f"""SELECT equipment_id, fixed_asset_no, asset_id
                FROM ext_asset_registry
                WHERE tenant_id=? AND ({clauses})
                ORDER BY equipment_id
                LIMIT 100""",
            (tenant_id, *params),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return None

        matches = {
            (row["equipment_id"], row["fixed_asset_no"], row["asset_id"])
            for row in rows
        }
        if len(matches) > 1:
            raise AssetRegistryConflict("Asset identifiers resolve to multiple assets")
        equipment, fixed, canonical_asset = next(iter(matches))
        for supplied, expected in (
            (equipment_id, equipment),
            (fixed_asset_no, fixed),
            (asset_id, canonical_asset),
        ):
            if supplied and supplied not in {equipment, fixed, canonical_asset}:
                raise AssetRegistryConflict("Asset identifiers do not agree")
        return ResolvedAsset(
            tenant_id=tenant_id,
            equipment_id=equipment,
            fixed_asset_no=fixed,
            asset_id=canonical_asset,
            registry_version="sqlite-fixture",
            resolved_at=utc_now(),
        )


class UnconfiguredAssetRegistryAdapter:
    """Fail-closed production adapter until the customer registry is wired."""

    async def resolve(self, **kwargs) -> ResolvedAsset | None:
        raise AssetRegistryUnavailable(
            "Asset Registry adapter is not configured"
        )


class HTTPAssetRegistryAdapter:
    """Whitelist HTTP resolver for the customer Asset Registry service."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def resolve(
        self,
        *,
        tenant_id: str,
        equipment_id: str | None = None,
        fixed_asset_no: str | None = None,
        asset_id: str | None = None,
    ) -> ResolvedAsset | None:
        params = {"tenantId": tenant_id}
        if equipment_id:
            params["equipmentId"] = equipment_id
        if fixed_asset_no:
            params["fixedAssetNo"] = fixed_asset_no
        if asset_id:
            params["assetId"] = asset_id
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/v1/assets/resolve",
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AssetRegistryUnavailable("Asset Registry request failed") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 409:
            raise AssetRegistryConflict("Asset identifiers conflict")
        if response.status_code >= 500:
            raise AssetRegistryUnavailable("Asset Registry service unavailable")
        if response.status_code >= 400:
            raise AssetRegistryInvalid("Asset identifier was rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AssetRegistryUnavailable("Asset Registry returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise AssetRegistryUnavailable("Asset Registry returned malformed JSON")
        equipment = payload.get("equipmentId", payload.get("equipment_id"))
        if not isinstance(equipment, str) or not equipment:
            raise AssetRegistryUnavailable("Asset Registry omitted equipmentId")
        return ResolvedAsset(
            tenant_id=tenant_id,
            equipment_id=equipment,
            fixed_asset_no=payload.get("fixedAssetNo", payload.get("fixed_asset_no")),
            asset_id=payload.get("assetId", payload.get("asset_id")),
            registry_version=payload.get("registryVersion", payload.get("registry_version")),
            resolved_at=utc_now(),
        )


_resolver_override: AssetRegistryAdapter | None = None


def set_asset_registry_adapter(adapter: AssetRegistryAdapter | None) -> None:
    """Install a test/integration adapter without changing production config."""
    global _resolver_override
    _resolver_override = adapter


def asset_registry_adapter(db) -> AssetRegistryAdapter:
    if _resolver_override is not None:
        return _resolver_override
    mode = os.environ.get("ENTERPRISE_ASSET_REGISTRY_MODE", "").strip().lower()
    base_url = os.environ.get("ENTERPRISE_ASSET_REGISTRY_BASE_URL", "").strip()
    if mode == "http" or (base_url and os.environ.get("ENTERPRISE_TEST_MODE") != "1"):
        if not base_url:
            raise AssetRegistryUnavailable("Asset Registry base URL is not configured")
        return HTTPAssetRegistryAdapter(
            base_url,
            token=os.environ.get("ENTERPRISE_ASSET_REGISTRY_TOKEN", "").strip() or None,
        )
    if mode in {"sqlite", "test", "fixture"} or (
        not mode and os.environ.get("ENTERPRISE_TEST_MODE") == "1"
    ):
        return SQLiteAssetRegistryAdapter(db)
    return UnconfiguredAssetRegistryAdapter()


async def resolve_asset(
    db,
    *,
    tenant_id: str,
    equipment_id: str | None = None,
    fixed_asset_no: str | None = None,
    asset_id: str | None = None,
) -> ResolvedAsset:
    if not any((equipment_id, fixed_asset_no, asset_id)):
        raise AssetRegistryInvalid("An equipment identifier is required")
    resolved = await asset_registry_adapter(db).resolve(
        tenant_id=tenant_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        asset_id=asset_id,
    )
    if resolved is None:
        raise AssetRegistryInvalid("Asset identifier was not found")
    return resolved
