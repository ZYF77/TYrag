# Development Asset Registry Stub

This is a temporary development stub for the Asset Registry boundary. It is
not a customer integration and must not be used to report formal
`Integration` acceptance as passed.

## Start

```powershell
$env:ENTERPRISE_ASSET_REGISTRY_BASE_URL = 'http://127.0.0.1:9390'
python enterprise/scripts/asset_registry_stub.py --port 9390
```

The default sample contains two independent tenant-scoped identities:

| tenantId | equipmentId | fixedAssetNo | assetId |
|---|---|---|---|
| `wp04e2e` | `EQ-E2E-001` | `FA-Doc1` | `FA-Doc1` |
| `wp04e2e` | `EQ-E2E-002` | `FA-Doc2` | `FA-Doc2` |

Use `--data-file <path>` or `ENTERPRISE_ASSET_REGISTRY_STUB_DATA` to provide a
JSON array with the same fields. Each record must contain non-empty
`tenantId`, `equipmentId`, and `fixedAssetNo`; `assetId` remains optional. The
stub rejects duplicate or conflicting mappings before serving requests. It
never prints the optional bearer token. If `ENTERPRISE_ASSET_REGISTRY_TOKEN`
is set, requests must use it.

## Temporary Protocol

Request:

```http
GET /v1/assets/resolve?tenantId=<tenant>&equipmentId=<equipment>&fixedAssetNo=<fixed>&assetId=<asset>
Accept: application/json
Authorization: Bearer <optional-token>
```

At least one of `equipmentId`, `fixedAssetNo`, or `assetId` is required.
Empty or whitespace-only identifiers are rejected.

Within one tenant, `equipmentId` and `fixedAssetNo` are a strict one-to-one
pair. Equipment-only, fixed-only, and consistent dual-identifier requests all
return the same canonical record. The tenant is part of the lookup scope, so
the same values may be used by another tenant without crossing identities.

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

| HTTP status | Meaning |
|---:|---|
| `200` | One canonical identity was resolved |
| `401` | Configured bearer token is missing or invalid |
| `404` | No identity exists in the requested tenant |
| `409` | Supplied identifiers resolve to different or multiple identities |
| `422` | Required tenant or identifier is missing or empty |

## Handoff Questions

- Is `tenantId` part of the resolver authorization scope or only a query field?
- Are `fixedAssetNo` and `assetId` tenant-scoped in the customer registry?
- Which authentication scheme and token audience should Gateway use?
- What stable version, ETag, or updated-at field identifies registry changes?
- Which error envelope and retry semantics are guaranteed?

Replace the stub with the real service before formal Integration acceptance.
