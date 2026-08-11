# Development Asset Registry Stub

This is a temporary development stub for the Asset Registry boundary. It is
not a customer integration and must not be used to report formal
`Integration` acceptance as passed.

## Start

```powershell
$env:ENTERPRISE_ASSET_REGISTRY_BASE_URL = 'http://127.0.0.1:9390'
python enterprise/scripts/asset_registry_stub.py --port 9390
```

The default data covers the current WP-04 scenario:

| tenantId | equipmentId | fixedAssetNo | assetId |
|---|---|---|---|
| `wp04e2e` | `EQ-E2E-001` | `FA-Doc1` | `FA-Doc1` |
| `wp04e2e` | `EQ-E2E-001` | `FA-Doc2` | `FA-Doc2` |

Use `--data-file <path>` or `ENTERPRISE_ASSET_REGISTRY_STUB_DATA` to provide a
JSON array with the same fields. The stub never prints the optional bearer
token. If `ENTERPRISE_ASSET_REGISTRY_TOKEN` is set, requests must use it.

## Temporary Protocol

Request:

```http
GET /v1/assets/resolve?tenantId=<tenant>&equipmentId=<equipment>&fixedAssetNo=<fixed>&assetId=<asset>
Accept: application/json
Authorization: Bearer <optional-token>
```

At least one of `equipmentId`, `fixedAssetNo`, or `assetId` is required.

Successful response, HTTP `200`:

```json
{
  "tenantId": "wp04e2e",
  "equipmentId": "EQ-E2E-001",
  "fixedAssetNo": "FA-Doc1",
  "assetId": "FA-Doc1",
  "registryVersion": "dev-stub-v1",
  "resolvedAt": "2026-01-01T00:00:00+00:00"
}
```

The current stub returns an equipment-level identity with null fixed-asset
fields when equipment-only input matches multiple fixed assets. This rule is
temporary and must be confirmed by the device management system owner.

| HTTP status | Meaning |
|---:|---|
| `200` | One canonical identity was resolved |
| `401` | Configured bearer token is missing or invalid |
| `404` | No identity exists in the requested tenant |
| `409` | Supplied identifiers resolve to different identities |
| `422` | Required tenant or identifier is missing |

## Handoff Questions

- Is `tenantId` part of the resolver authorization scope or only a query field?
- Is equipment-only resolution allowed when multiple fixed assets exist?
- Are `fixedAssetNo` and `assetId` globally unique or tenant-scoped?
- Which authentication scheme and token audience should Gateway use?
- What stable version, ETag, or updated-at field identifies registry changes?
- Which error envelope and retry semantics are guaranteed?

Replace the stub with the real service before formal Integration acceptance.
