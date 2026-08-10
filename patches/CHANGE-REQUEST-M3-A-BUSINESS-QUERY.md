# CHANGE-REQUEST: M3-A Business PostgreSQL / TimeSeries adapter

## Status

Open. This is an Enterprise-owned, internal adapter slice. It does not change
the public v2 OpenAPI contract and must not be used as evidence of Integration
acceptance while M2 is blocked.

## Missing upstream/customer contracts

- The repository has no frozen customer business schema for equipment, repair,
  maintenance, fault-history or time-series tables.
- `enterprise/requirements.txt` has no PostgreSQL driver, and adding one would
  require a dependency/security review and a lock-file owner.
- No customer Asset/permission service contract currently resolves business
  record ACL facts beyond the existing authenticated department claims.
- RAGFlow public connectors are ingestion/search integrations; they are not a
  query-time connector for the customer business database. The adapter does
  not use RAGFlow's internal database or document engine.

## Minimal implementation

1. `business_adapter.py` exposes strict operation requests rather than SQL.
   The only operations are equipment summary, recent repairs, recent
   maintenance, fault history and bounded time-series measurements.
2. SQL templates, selected fields, record identifiers and metric values are
   code-owned whitelists. Every statement is checked as a single `SELECT` and
   every value is passed through transport parameters.
3. The adapter requires a non-empty `BusinessAclScope` containing the
   authenticated tenant, departments and canonical equipment. It injects the
   tenant and department predicates and rejects out-of-scope rows instead of
   filtering them after retrieval.
4. Transport calls receive `readonly=True`, a bounded timeout and a bounded
   result limit. A replaceable `ReadonlyQueryTransport` is used because the
   real driver/schema contract is missing. The default transport fails closed.
5. Output projection drops fields outside the approved output schema. Audit
   events contain only adapter, operation, tenant, row count, limit, outcome
   and duration metadata; they never contain SQL, credentials or row content.

## Configuration

- `ENTERPRISE_BUSINESS_QUERY_ENABLED` and
  `ENTERPRISE_TIMESERIES_QUERY_ENABLED` default to `false`.
- Enabling PostgreSQL requires `PG_DATABASE`, `PG_USER` and an injected
  `ENTERPRISE_BUSINESS_QUERY_TRANSPORT=external` transport.
- `ENTERPRISE_BUSINESS_QUERY_MAX_ROWS`,
  `ENTERPRISE_BUSINESS_QUERY_MAX_RANGE_DAYS`,
  `ENTERPRISE_TIMESERIES_TIMEOUT` and
  `ENTERPRISE_TIMESERIES_MAX_RANGE_HOURS` are validated and bounded.
- No credential, token, password or customer data is stored in this change.

## Promotion conditions

- Freeze the customer schema, output/data classification, canonical equipment
  ACL resolver and driver/connection-pool contract in a separate change.
- Add a reviewed read-only transport implementation and real PostgreSQL and
  time-series integration tests using non-sensitive fixtures.
- Define the public business-record citation and error contract before wiring a
  route or updating the public OpenAPI document.
- Complete M2's real environment gate. Unit and transport-contract tests in
  this commit are not an Integration pass and use no skip/xfail workaround.

## Compatibility and rollback

- No `ragflow/**`, official migration, root lock file, main OpenAPI contract,
  document-engine abstraction or official database model is modified.
- No public route is registered; removing the adapter, tests and this request
  leaves existing document, ACL and conversation behavior unchanged.
