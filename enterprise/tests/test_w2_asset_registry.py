from __future__ import annotations

from enterprise.gateway.db.dialect import exec_sql

from enterprise.gateway.db.ops import gw_read, gw_write

import httpx
import pytest

from enterprise.gateway.asset_registry import (
    AssetRegistryAmbiguous,
    AssetRegistryConflict,
    AssetRegistryInvalid,
    AssetRegistryUnavailable,
    EAMAssetResolverAdapter,
    ResolvedAsset,
    resolve_asset,
    set_asset_registry_adapter,
)


async def _insert_registry_row(
    db,
    *,
    tenant_id: str,
    equipment_id: str,
    fixed_asset_no: str | None,
    asset_id: str | None = None,
) -> None:
    await gw_write(db, exec_sql, """INSERT INTO ext_asset_registry
           (tenant_id, equipment_id, fixed_asset_no, asset_id)
           VALUES (?, ?, ?, ?)""",
        (tenant_id, equipment_id, fixed_asset_no, asset_id or fixed_asset_no),
    )


@pytest.mark.asyncio
async def test_single_and_double_identifiers_resolve_to_one_canonical_identity(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    await _insert_registry_row(
        db,
        tenant_id="tenant-a",
        equipment_id="EQ-1",
        fixed_asset_no="FA-1",
        asset_id="ASSET-1",
    )

    equipment_only = await resolve_asset(
        db, tenant_id="tenant-a", equipment_id="EQ-1"
    )
    fixed_only = await resolve_asset(
        db, tenant_id="tenant-a", fixed_asset_no="FA-1"
    )
    both = await resolve_asset(
        db,
        tenant_id="tenant-a",
        equipment_id="EQ-1",
        fixed_asset_no="FA-1",
    )

    assert (equipment_only.equipment_id, equipment_only.fixed_asset_no) == (
        "EQ-1",
        "FA-1",
    )
    assert [
        (item.tenant_id, item.equipment_id, item.fixed_asset_no, item.asset_id)
        for item in (fixed_only, both, equipment_only)
    ] == [
        ("tenant-a", "EQ-1", "FA-1", "ASSET-1"),
        ("tenant-a", "EQ-1", "FA-1", "ASSET-1"),
        ("tenant-a", "EQ-1", "FA-1", "ASSET-1"),
    ]


@pytest.mark.asyncio
async def test_fixed_asset_reuse_is_rejected_within_tenant_but_isolated_across_tenants(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    await _insert_registry_row(
        db,
        tenant_id="tenant-a",
        equipment_id="EQ-A",
        fixed_asset_no="FA-REUSED",
    )
    await _insert_registry_row(
        db,
        tenant_id="tenant-a",
        equipment_id="EQ-B",
        fixed_asset_no="FA-REUSED",
    )
    with pytest.raises(AssetRegistryConflict):
        await resolve_asset(db, tenant_id="tenant-a", fixed_asset_no="FA-REUSED")

    await _insert_registry_row(
        db,
        tenant_id="tenant-b",
        equipment_id="EQ-C",
        fixed_asset_no="FA-REUSED",
    )
    isolated = await resolve_asset(
        db, tenant_id="tenant-b", fixed_asset_no="FA-REUSED"
    )
    assert (isolated.equipment_id, isolated.fixed_asset_no) == (
        "EQ-C",
        "FA-REUSED",
    )

    with pytest.raises(AssetRegistryConflict):
        await resolve_asset(
            db,
            tenant_id="tenant-a",
            equipment_id="EQ-A",
            fixed_asset_no="FA-REUSED",
        )


@pytest.mark.asyncio
async def test_equipment_with_multiple_fixed_assets_is_ambiguous(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    await gw_write(db, exec_sql, "DROP TABLE ext_asset_registry")
    await gw_write(db, exec_sql, """CREATE TABLE ext_asset_registry (
               tenant_id TEXT NOT NULL,
               equipment_id TEXT NOT NULL,
               fixed_asset_no TEXT,
               asset_id TEXT
           )""")
    for fixed_asset_no, asset_id in [("FA-1", "ASSET-1"), ("FA-2", "ASSET-2")]:
        await gw_write(
            db,
            exec_sql,
            """INSERT INTO ext_asset_registry
               (tenant_id, equipment_id, fixed_asset_no, asset_id)
               VALUES ('tenant-a', 'EQ-A', ?, ?)""",
            (fixed_asset_no, asset_id),
        )

    with pytest.raises(AssetRegistryAmbiguous):
        await resolve_asset(db, tenant_id="tenant-a", equipment_id="EQ-A")
    fixed_only = await resolve_asset(
        db, tenant_id="tenant-a", fixed_asset_no="FA-1"
    )
    assert (fixed_only.equipment_id, fixed_only.fixed_asset_no) == (
        "EQ-A",
        "FA-1",
    )
    with pytest.raises(AssetRegistryAmbiguous):
        await resolve_asset(
            db,
            tenant_id="tenant-a",
            equipment_id="EQ-A",
            fixed_asset_no="FA-1",
        )


@pytest.mark.asyncio
async def test_not_found_missing_and_mapping_drift_are_rejected(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    await _insert_registry_row(
        db,
        tenant_id="tenant-a",
        equipment_id="EQ-DRIFT",
        fixed_asset_no="FA-OLD",
    )
    with pytest.raises(AssetRegistryInvalid):
        await resolve_asset(db, tenant_id="tenant-a", equipment_id="EQ-MISSING")
    with pytest.raises(AssetRegistryInvalid):
        await resolve_asset(db, tenant_id="tenant-a")

    await gw_write(db, exec_sql, """UPDATE ext_asset_registry SET fixed_asset_no='FA-NEW'
           WHERE tenant_id='tenant-a' AND equipment_id='EQ-DRIFT'""")
    with pytest.raises(AssetRegistryConflict):
        await resolve_asset(
            db,
            tenant_id="tenant-a",
            equipment_id="EQ-DRIFT",
            fixed_asset_no="FA-OLD",
        )


@pytest.mark.asyncio
async def test_registry_unavailable_and_wrong_tenant_fail_closed(
    isolated_gateway_db, monkeypatch
):
    db, _ = isolated_gateway_db
    monkeypatch.setenv("ENTERPRISE_EAM_ASSET_RESOLVER_MODE", "unconfigured")
    with pytest.raises(AssetRegistryUnavailable):
        await resolve_asset(db, tenant_id="tenant-a", equipment_id="EQ-1")

    class WrongTenantAdapter:
        async def resolve(self, **kwargs):
            return ResolvedAsset(
                tenant_id="tenant-b",
                equipment_id=kwargs["equipment_id"],
                fixed_asset_no="FA-1",
                asset_id="ASSET-1",
                registry_version="test",
                resolved_at="2026-08-12T00:00:00+00:00",
            )

    set_asset_registry_adapter(WrongTenantAdapter())
    try:
        with pytest.raises(AssetRegistryConflict):
            await resolve_asset(
                db, tenant_id="tenant-a", equipment_id="EQ-1"
            )
    finally:
        set_asset_registry_adapter(None)


@pytest.mark.asyncio
async def test_eam_asset_resolver_uses_three_identifiers_without_tenant_query(
    monkeypatch,
):
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "equipmentId": "EQ-1",
                "fixedAssetNo": "FA-1",
                "assetId": "ASSET-1",
            }

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 5

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, params, headers):
            calls.append({"url": url, "params": params, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    adapter = EAMAssetResolverAdapter(
        "https://eam.example.test",
        path="/api/integration/v1/assets/resolve",
        token="test-token",
    )

    resolved = await adapter.resolve(
        tenant_id="wp04e2e",
        equipment_id="EQ-1",
        fixed_asset_no="FA-1",
        asset_id="ASSET-1",
    )

    assert resolved is not None
    assert (resolved.tenant_id, resolved.equipment_id) == ("wp04e2e", "EQ-1")
    assert calls[0]["url"] == "https://eam.example.test/api/integration/v1/assets/resolve"
    assert calls[0]["params"] == {
        "equipmentId": "EQ-1",
        "fixedAssetNo": "FA-1",
        "assetId": "ASSET-1",
    }
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert "tenantId" not in calls[0]["params"]
