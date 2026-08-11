# TASK-P0-A contract change request

## Scope

The FILE_SHARE v3 document status response needs an additive schema update.
This branch does not modify `contracts/**`; the main thread should apply the
OpenAPI change.

The existing status resource remains:

`GET /enterprise/api/v3/documents/{externalDocumentId}/status?tenantId=...&sourceSystem=...&sourceVersionId=...`

Every `202` response from `POST /enterprise/api/v3/documents` returns a
relative `statusUrl` pointing to that exact resource. The path and query
components are percent-encoded by the server.

## Additive response schema

Add these fields to the existing status payload:

```yaml
statusUrl:
  type: string
  description: Stable relative URL for this exact tenant/source/document/version status resource.
retrievable:
  type: boolean
  description: Document-level retrieval-candidate readiness only; user ACL is not included.
readiness:
  type: object
  required: [currentVersion, active, syncReady, parserReadback, ragflowIdsPresent, qualityPassed, blockingReason]
  properties:
    currentVersion: { type: boolean }
    active: { type: boolean }
    syncReady: { type: boolean }
    parserReadback: { type: boolean }
    ragflowIdsPresent: { type: boolean }
    qualityPassed: { type: boolean }
    blockingReason:
      type: string
      nullable: true
qualityStatus:
  type: string
  nullable: true
error:
  type: object
  nullable: true
  properties:
    code: { type: string }
    message: { type: string }
    retryable: { type: boolean }
```

Example accepted-but-not-ready response:

```json
{
  "operationId": "evt-001",
  "externalDocumentId": "manual/001",
  "sourceVersionId": "v1",
  "statusUrl": "/enterprise/api/v3/documents/manual%2F001/status?tenantId=tenant-a&sourceSystem=DEMO&sourceVersionId=v1",
  "status": "received",
  "retrievable": false,
  "readiness": {
    "currentVersion": false,
    "active": true,
    "syncReady": false,
    "parserReadback": false,
    "ragflowIdsPresent": true,
    "qualityPassed": false,
    "blockingReason": "DOCUMENT_NOT_CURRENT_VERSION"
  },
  "qualityStatus": null,
  "error": null
}
```

## Compatibility and invariants

- The change is additive; existing consumers may ignore the new fields.
- `statusUrl` is relative and server-generated; clients must not reconstruct it.
- No second business route and no `Location` header are introduced.
- `retrievable` is the same document-candidate gate used by the formal
  `/enterprise/api/v2` query before user ACL evaluation. It does not persist or
  represent any user's ACL result.
- `status`, message business status, and citations remain independent.
- Error messages are stable and sanitized; raw sync error details are not
  exposed.
