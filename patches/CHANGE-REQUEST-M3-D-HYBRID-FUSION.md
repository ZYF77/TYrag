# CHANGE-REQUEST: M3-D Hybrid Fusion Core

## Status

Open. The implementation is an Enterprise-internal, dependency-free core for
P1 preparation. It does not change the public v2 OpenAPI or claim Integration
acceptance.

## Scope

- Accept ranked dense and sparse hits through a typed internal boundary.
- Apply tenant equality and frozen document ACL before fusion.
- Combine authorized hits with weighted reciprocal-rank fusion (RRF),
  deterministic duplicate handling, payload preservation, and stable tie-breaks.
- Fail closed for inactive principals, missing ACL facts, invalid scores, and
  invalid policy parameters.

## Contract and Integration

- No public route, OpenAPI schema, error-code baseline, RAGFlow code, migration,
  or lock file was changed.
- The core is not wired into the P0 conversation/query route. A future adapter
  must map an approved RAGFlow search contract into `HybridHit` without
  exposing internal identifiers through the public v2 envelope.
- Unit tests use deterministic in-process values only and are not Integration
  evidence for RAGFlow, Redis, or a business retrieval backend.

## Promotion Conditions

Before public use, define the dense/sparse provider contract, score semantics,
ACL-facts ownership, observability fields, timeout/retry policy, and real
backend evaluation thresholds in a separate P1 contract update.

## Rollback

Remove the hybrid module, its tests, and this change request. Existing query,
document, ACL, and RAGFlow behavior is unaffected because no router imports the
core.
