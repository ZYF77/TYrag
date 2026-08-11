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
            "equipmentId": "EQ-001",
            "fixedAssetNo": "FA-002",
            "assetId": "ASSET-002",
            "registryVersion": "registry-test-v1",
        },
    ]


@pytest.mark.asyncio
async def test_equipment_only_resolution_returns_parent_identity():
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
        "fixedAssetNo": None,
        "assetId": None,
        "registryVersion": "dev-stub-v1",
        "resolvedAt": response.json()["resolvedAt"],
    }
    assert response.headers["X-TYRAG-ASSET-REGISTRY-IMPLEMENTATION"] == "dev-stub"


@pytest.mark.asyncio
async def test_specific_asset_resolution_and_conflicts():
    app = create_app(_records())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        resolved = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "fixedAssetNo": "FA-002"},
        )
        resolved_with_parent = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-a",
                "equipmentId": "EQ-001",
                "fixedAssetNo": "FA-002",
            },
        )
        conflict = await client.get(
            "/v1/assets/resolve",
            params={
                "tenantId": "tenant-a",
                "equipmentId": "EQ-001",
                "fixedAssetNo": "FA-999",
            },
        )

    assert resolved.status_code == 200
    assert resolved.json()["assetId"] == "ASSET-002"
    assert resolved_with_parent.status_code == 200
    assert resolved_with_parent.json()["assetId"] == "ASSET-002"
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASSET_IDENTIFIER_CONFLICT"


@pytest.mark.asyncio
async def test_not_found_and_authentication_are_explicit():
    app = create_app(_records(), token="stub-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://asset-registry.test"
    ) as client:
        unauthorized = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-001"},
        )
        missing = await client.get(
            "/v1/assets/resolve",
            params={"tenantId": "tenant-a", "equipmentId": "EQ-404"},
            headers={"Authorization": "Bearer stub-token"},
        )

    assert unauthorized.status_code == 401
    assert missing.status_code == 404
    assert missing.json()["code"] == "ASSET_NOT_FOUND"
