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


class AssetRegistryAmbiguous(AssetRegistryConflict):
    """The supplied identifier maps to more than one registry identity."""


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


def _validate_lookup(
    *,
    tenant_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    asset_id: str | None,
) -> tuple[str, str | None, str | None, str | None]:
    values = {
        "tenant_id": tenant_id,
        "equipment_id": equipment_id,
        "fixed_asset_no": fixed_asset_no,
        "asset_id": asset_id,
    }
    for name, value in values.items():
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise AssetRegistryInvalid(f"{name} must be a non-empty string")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise AssetRegistryInvalid("tenant_id must be a non-empty string")
    if not any(value is not None for value in (equipment_id, fixed_asset_no, asset_id)):
        raise AssetRegistryInvalid("An equipment identifier is required")
    return tenant_id, equipment_id, fixed_asset_no, asset_id


def _row_identity(row) -> tuple[str, str, str | None, str | None]:
    tenant_id = row["tenant_id"]
    equipment_id = row["equipment_id"]
    fixed_asset_no = row["fixed_asset_no"]
    asset_id = row["asset_id"]
    if not isinstance(tenant_id, str) or not tenant_id:
        raise AssetRegistryUnavailable("Asset Registry returned an invalid tenant")
    if not isinstance(equipment_id, str) or not equipment_id:
        raise AssetRegistryUnavailable("Asset Registry omitted equipmentId")
    for name, value in (
        ("fixedAssetNo", fixed_asset_no),
        ("assetId", asset_id),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise AssetRegistryUnavailable(f"Asset Registry returned an invalid {name}")
    return tenant_id, equipment_id, fixed_asset_no, asset_id


def _identity_set(rows) -> set[tuple[str, str, str | None, str | None]]:
    return {_row_identity(row) for row in rows}


def _validate_resolved(
    resolved: ResolvedAsset,
    *,
    tenant_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    asset_id: str | None,
) -> ResolvedAsset:
    if not isinstance(resolved, ResolvedAsset):
        raise AssetRegistryUnavailable("Asset Registry returned an invalid identity")
    if not isinstance(resolved.tenant_id, str) or not resolved.tenant_id:
        raise AssetRegistryUnavailable("Asset Registry omitted tenantId")
    if resolved.tenant_id != tenant_id:
        raise AssetRegistryConflict("Asset Registry tenant mismatch")
    if not isinstance(resolved.equipment_id, str) or not resolved.equipment_id:
        raise AssetRegistryUnavailable("Asset Registry omitted equipmentId")
    for name, value in (
        ("fixedAssetNo", resolved.fixed_asset_no),
        ("assetId", resolved.asset_id),
        ("registryVersion", resolved.registry_version),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise AssetRegistryUnavailable(
                f"Asset Registry returned an invalid {name}"
            )
    if not isinstance(resolved.resolved_at, str) or not resolved.resolved_at:
        raise AssetRegistryUnavailable("Asset Registry omitted resolvedAt")
    for supplied, canonical in (
        (equipment_id, resolved.equipment_id),
        (fixed_asset_no, resolved.fixed_asset_no),
        (asset_id, resolved.asset_id),
    ):
        if supplied is not None and supplied != canonical:
            raise AssetRegistryConflict("Asset identifiers do not agree")
    return resolved


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
        tenant_id, equipment_id, fixed_asset_no, asset_id = _validate_lookup(
            tenant_id=tenant_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
            asset_id=asset_id,
        )
        async with self.db.execute(
            """SELECT tenant_id, equipment_id, fixed_asset_no, asset_id
                FROM ext_asset_registry
                WHERE tenant_id=?
                ORDER BY equipment_id""",
            (tenant_id,),
        ) as cursor:
            tenant_rows = await cursor.fetchall()

        fields = (
            ("equipment_id", equipment_id),
            ("fixed_asset_no", fixed_asset_no),
            ("asset_id", asset_id),
        )
        match_sets = [
            {
                index
                for index, row in enumerate(tenant_rows)
                if row[field] == value
            }
            for field, value in fields
            if value is not None
        ]

        if not match_sets:
            return None
        if any(not matches for matches in match_sets):
            if any(match_sets):
                raise AssetRegistryConflict("Asset identifiers do not agree")
            return None

        for (field, value), matches in zip(
            (item for item in fields if item[1] is not None), match_sets
        ):
            identities = _identity_set([tenant_rows[index] for index in matches])
            if len(identities) > 1:
                raise AssetRegistryAmbiguous(
                    f"{field} resolves to multiple assets"
                )

        candidate_indexes = set.intersection(*match_sets)
        if not candidate_indexes:
            raise AssetRegistryConflict("Asset identifiers do not agree")
        candidate_rows = [tenant_rows[index] for index in candidate_indexes]

        identities = _identity_set(candidate_rows)
        if len(identities) != 1:
            raise AssetRegistryAmbiguous(
                "Asset identifiers resolve to multiple assets"
            )
        _, equipment, fixed, canonical_asset = next(iter(identities))
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
        if response.status_code in (408, 425, 429) or response.status_code >= 500:
            raise AssetRegistryUnavailable("Asset Registry service unavailable")
        if response.status_code >= 400:
            raise AssetRegistryInvalid("Asset identifier was rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AssetRegistryUnavailable("Asset Registry returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise AssetRegistryUnavailable("Asset Registry returned malformed JSON")
        response_tenant = payload.get("tenantId", payload.get("tenant_id"))
        if not isinstance(response_tenant, str) or not response_tenant:
            raise AssetRegistryUnavailable("Asset Registry omitted tenantId")
        if response_tenant != tenant_id:
            raise AssetRegistryConflict("Asset Registry tenant mismatch")
        equipment = payload.get("equipmentId", payload.get("equipment_id"))
        if not isinstance(equipment, str) or not equipment:
            raise AssetRegistryUnavailable("Asset Registry omitted equipmentId")
        fixed = payload.get("fixedAssetNo", payload.get("fixed_asset_no"))
        canonical_asset = payload.get("assetId", payload.get("asset_id"))
        registry_version = payload.get(
            "registryVersion", payload.get("registry_version")
        )
        resolved_at = payload.get("resolvedAt", payload.get("resolved_at"))
        if resolved_at is None:
            raise AssetRegistryUnavailable("Asset Registry omitted resolvedAt")
        return ResolvedAsset(
            tenant_id=tenant_id,
            equipment_id=equipment,
            fixed_asset_no=fixed,
            asset_id=canonical_asset,
            registry_version=registry_version,
            resolved_at=resolved_at,
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
    tenant_id, equipment_id, fixed_asset_no, asset_id = _validate_lookup(
        tenant_id=tenant_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        asset_id=asset_id,
    )
    resolved = await asset_registry_adapter(db).resolve(
        tenant_id=tenant_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        asset_id=asset_id,
    )
    if resolved is None:
        raise AssetRegistryInvalid("Asset identifier was not found")
    return _validate_resolved(
        resolved,
        tenant_id=tenant_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        asset_id=asset_id,
    )
