from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise.scripts.asset_registry_stub import create_app


def _records():
    return [
        {
            "tenantId": "tenant-a",
            "equipmentId": "EQ-001",
            "fixedAssetNo": "FA-001",
            "assetId": "ASSET-001",
            "registryVersion": "registry-test-v1",
        },
        {
            "tenantId": "tenant-a",
            "equipmentId": "EQ-002",
            "fixedAssetNo": "FA-002",
            "assetId": "ASSET-002",
            "registryVersion": "registry-test-v1",
        },
        {
            "tenantId": "tenant-b",
            "equipmentId": "EQ-001",
            "fixedAssetNo": "FA-001",
            "assetId": "ASSET-TENANT-B-001",
            "registryVersion": "registry-test-v1",
        },
    ]


@pytest.mark.asyncio
async def test_equipment_only_resolution_returns_canonical_identity():
    app = create_app(_records())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        response = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-001"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "tenantId": "tenant-a",
        "equipmentId": "EQ-001",
        "fixedAssetNo": "FA-001",
        "assetId": "ASSET-001",
        "registryVersion": "registry-test-v1",
        "resolvedAt": response.json()["resolvedAt"],
    }
    assert response.headers["X-TYRAG-ASSET-REGISTRY-IMPLEMENTATION"] == "dev-stub"


@pytest.mark.asyncio
async def test_fixed_only_and_consistent_dual_identifier_resolution():
    app = create_app(_records())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        resolved = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "fixedAssetNo": "FA-002"},
        )
        resolved_with_both_identifiers = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-a",
                "equipmentId": "EQ-002",
                "fixedAssetNo": "FA-002",
            },
        )

    assert resolved.status_code == 200
    assert resolved.json()["assetId"] == "ASSET-002"
    assert resolved_with_both_identifiers.status_code == 200
    assert resolved_with_both_identifiers.json()["equipmentId"] == "EQ-002"
    assert resolved_with_both_identifiers.json()["assetId"] == "ASSET-002"


@pytest.mark.asyncio
async def test_conflicts_unknown_empty_and_tenant_isolation_are_fail_closed():
    app = create_app(_records())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        conflict = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-a",
                "equipmentId": "EQ-001",
                "fixedAssetNo": "FA-002",
            },
        )
        unknown_equipment = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-404"},
        )
        unknown_fixed = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "fixedAssetNo": "FA-404"},
        )
        empty_identifier = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": ""},
        )
        mixed_empty_identifier = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-a",
                "equipmentId": "EQ-001",
                "fixedAssetNo": " ",
            },
        )
        tenant_isolated = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-b",
                "equipmentId": "EQ-001",
                "fixedAssetNo": "FA-001",
            },
        )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASSET_IDENTIFIER_CONFLICT"
    assert unknown_equipment.status_code == 404
    assert unknown_fixed.status_code == 404
    assert empty_identifier.status_code == 422
    assert empty_identifier.json()["code"] == "ASSET_IDENTIFIER_REQUIRED"
    assert mixed_empty_identifier.status_code == 422
    assert tenant_isolated.status_code == 200
    assert tenant_isolated.json()["tenantId"] == "tenant-b"
    assert tenant_isolated.json()["assetId"] == "ASSET-TENANT-B-001"


@pytest.mark.parametrize(
    "invalid_records",
    [
        [
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": "FA-001"},
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": "FA-002"},
        ],
        [
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": "FA-001"},
            {"tenantId": "tenant-a", "equipmentId": "EQ-002", "fixedAssetNo": "FA-001"},
        ],
        [
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": "FA-001"},
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": "FA-001"},
        ],
        [
            {"tenantId": "tenant-a", "equipmentId": "EQ-001", "fixedAssetNo": None},
        ],
    ],
)
def test_invalid_fixture_is_rejected_before_app_starts(invalid_records):
    with pytest.raises(ValueError):
        create_app(invalid_records)


@pytest.mark.asyncio
async def test_health_and_authentication_remain_explicit():
    app = create_app(_records(), token="stub-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        health = await client.get("/health")
        unauthorized = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-001"},
        )
        missing = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-404"},
            headers={"Authorization": "Bearer stub-token"},
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "implementation": "dev-stub"}
    assert unauthorized.status_code == 401
    assert missing.status_code == 404
    assert missing.json()["code"] == "ASSET_NOT_FOUND"
