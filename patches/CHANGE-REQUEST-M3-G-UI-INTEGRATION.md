# M3-G Contract Change Request

## Status

Proposed only. No main OpenAPI, shared contract, gateway, or upstream file is changed by M3-G.

## Gaps Found

1. `integration-openapi-v2.yaml` returns `fixedAssetNo` and `registryVersion` in the conversation context, but does not expose the canonical `assetId` that the Gateway Asset Registry snapshot already carries. The UI can therefore show `assetId` only when it is present in an authorized citation snapshot.
2. The v2 transient attachment endpoint is explicitly P1/planned and documents only the `501` error response. Expiry semantics and a stable `ATTACHMENT_EXPIRED` error code are not frozen. The UI treats these as Gateway `ErrorResponse` values and its expiry case is a UI contract fixture only.

## Requested Additive Changes

- Add nullable `assetId` to the v2 conversation context/detail response as a server-resolved, read-only field. Do not accept it as a client-controlled context override.
- Define the transient attachment success receipt, expiry response/status, stable error code, and whether expiry is `404` or `410`; retain `indexPolicy=never`, conversation scope, and the 24-hour TTL.

## Compatibility And Rollback

These are additive response/error-contract changes. Until Lead freezes them, the frontend must not claim support beyond the existing v2 wire contract. Rollback is removal of the additive response field and attachment UI once the Gateway contract decision is rejected.
