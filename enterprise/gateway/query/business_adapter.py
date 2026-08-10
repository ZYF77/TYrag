"""Read-only whitelist adapters for business PostgreSQL and time-series data.

The business database is a query-time source, not a RAGFlow or Gateway state
store.  This module intentionally exposes operations rather than SQL to its
callers.  A deployment supplies a transport implementation; no PostgreSQL
driver is imported here because the repository does not currently freeze a
customer business schema or driver contract.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.gateway.acl.context import AclContext

logger = logging.getLogger(__name__)

POSTGRESQL = "postgresql"
TIMESERIES = "timeseries"
BackendName = Literal["postgresql", "timeseries"]

POSTGRES_OPERATIONS = frozenset(
    {
        "get_equipment_summary",
        "list_recent_repairs",
        "list_recent_maintenance",
        "get_fault_history",
    }
)
TIMESERIES_OPERATIONS = frozenset({"list_measurements"})
ALLOWED_METRICS = frozenset({"temperature", "vibration", "pressure", "energy"})


class BusinessQueryError(RuntimeError):
    """Base error with a safe, client-independent error code."""

    def __init__(self, code: str, message: str = "Business query failed") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BusinessQueryConfigurationError(BusinessQueryError):
    """The adapter cannot be safely constructed from the deployment config."""


class BusinessQueryValidationError(BusinessQueryError):
    """The operation or its bounded input is not part of the whitelist."""


class BusinessQueryPermissionError(BusinessQueryError):
    """The request has no usable tenant-scoped business ACL scope."""


class BusinessQueryUnavailableError(BusinessQueryError):
    """The external read-only transport is not available."""


class BusinessQueryTimeoutError(BusinessQueryError):
    """The bounded database operation exceeded its timeout."""


class BusinessQueryScopeViolation(BusinessQueryError):
    """The transport returned a row outside the authorized scope."""


class BusinessQueryResultError(BusinessQueryError):
    """The transport returned a row that does not satisfy its contract."""


class BusinessQueryRequest(BaseModel):
    """Strict operation request; it has no tenant or SQL fields by design."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    backend: BackendName
    operation: str = Field(min_length=1, max_length=64)
    equipment_id: str = Field(min_length=1, max_length=128)
    fault_code: str | None = Field(default=None, min_length=1, max_length=128)
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    metric: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_whitelisted_operation(self) -> "BusinessQueryRequest":
        operations = (
            POSTGRES_OPERATIONS
            if self.backend == POSTGRESQL
            else TIMESERIES_OPERATIONS
        )
        if self.operation not in operations:
            raise ValueError("operation is not supported for this backend")

        if self.operation == "get_fault_history" and (
            not self.fault_code or self.from_ is None or self.to is None
        ):
            raise ValueError("fault history requires faultCode, from and to")
        if self.operation == "list_measurements" and (
            not self.metric or self.from_ is None or self.to is None
        ):
            raise ValueError("time-series measurements require metric, from and to")
        if self.operation != "get_fault_history" and self.fault_code is not None:
            raise ValueError("faultCode is not accepted by this operation")
        if self.operation not in {"get_fault_history", "list_measurements"} and (
            self.from_ is not None or self.to is not None
        ):
            raise ValueError("from and to are not accepted by this operation")
        if self.operation != "list_measurements" and self.metric is not None:
            raise ValueError("metric is not accepted by this operation")
        if self.metric is not None and self.metric not in ALLOWED_METRICS:
            raise ValueError("metric is not whitelisted")

        for value in (self.from_, self.to):
            if value is not None and value.tzinfo is None:
                raise ValueError("timestamps must include a timezone")
        if self.from_ is not None and self.to is not None and self.to <= self.from_:
            raise ValueError("to must be after from")
        return self


@dataclass(frozen=True)
class BusinessAclScope:
    """The server-side tenant, department and canonical equipment boundary."""

    tenant_id: str
    department_ids: tuple[str, ...]
    equipment_ids: tuple[str, ...]
    policy_version: str = ""

    @classmethod
    def empty(cls, policy_version: str = "") -> "BusinessAclScope":
        return cls("", (), (), policy_version)

    @property
    def is_empty(self) -> bool:
        return not (
            self.tenant_id
            and self.department_ids
            and self.equipment_ids
        )

    def permits(self, equipment_id: str) -> bool:
        return not self.is_empty and equipment_id in self.equipment_ids


class BusinessScopeResolver(Protocol):
    """Resolves canonical equipment access from an authenticated ACL context."""

    async def resolve(
        self,
        context: AclContext,
        equipment_id: str,
    ) -> BusinessAclScope:
        ...


class PrincipalBusinessScopeResolver:
    """Minimal resolver for a trusted canonical equipment snapshot.

    The resolver uses department claims only as the local ACL boundary.  A
    production deployment can replace it with its permission-service resolver
    without changing the adapter or query contract.
    """

    async def resolve(
        self,
        context: AclContext,
        equipment_id: str,
    ) -> BusinessAclScope:
        principal = context.principal if context else None
        if (
            principal is None
            or not principal.is_active
            or not principal.tenant_id
            or not equipment_id
            or not principal.department_ids
        ):
            return BusinessAclScope.empty(context.policy_version if context else "")
        return BusinessAclScope(
            tenant_id=principal.tenant_id,
            department_ids=tuple(principal.department_ids),
            equipment_ids=(equipment_id,),
            policy_version=context.policy_version,
        )


async def compile_business_scope(
    context: AclContext | None,
    equipment_id: str,
    resolver: BusinessScopeResolver | None = None,
) -> BusinessAclScope:
    """Compile a business scope and fail closed on resolver or identity errors."""

    policy_version = context.policy_version if context else ""
    if (
        context is None
        or context.principal is None
        or not context.principal.is_active
        or not context.principal.tenant_id
        or not equipment_id
        or resolver is None
    ):
        return BusinessAclScope.empty(policy_version)
    try:
        scope = await resolver.resolve(context, equipment_id)
    except Exception:
        logger.warning("Business ACL scope resolution failed; denying query")
        return BusinessAclScope.empty(policy_version)
    if not isinstance(scope, BusinessAclScope) or scope.is_empty:
        return BusinessAclScope.empty(policy_version)
    if scope.tenant_id != context.principal.tenant_id:
        logger.warning("Business ACL resolver returned a tenant mismatch")
        return BusinessAclScope.empty(policy_version)
    if equipment_id not in scope.equipment_ids:
        logger.warning("Business ACL resolver omitted requested equipment")
        return BusinessAclScope.empty(policy_version)
    if any(not value for value in scope.department_ids + scope.equipment_ids):
        logger.warning("Business ACL resolver returned an invalid scope")
        return BusinessAclScope.empty(policy_version)
    return scope


class ReadonlyQueryTransport(Protocol):
    """Replaceable driver boundary; callers cannot submit arbitrary SQL input."""

    async def fetch(
        self,
        *,
        sql: str,
        params: tuple[Any, ...],
        timeout_seconds: float,
        readonly: bool,
    ) -> Sequence[Mapping[str, Any]]:
        ...


class QueryAuditSink(Protocol):
    """Receives safe query metadata, never row contents or credentials."""

    async def record(self, event: "BusinessQueryAuditEvent") -> None:
        ...


class NoopQueryAuditSink:
    async def record(self, event: "BusinessQueryAuditEvent") -> None:
        return None


@dataclass(frozen=True)
class BusinessQueryAuditEvent:
    backend: str
    operation: str
    tenant_id: str
    requested_limit: int
    row_count: int
    duration_ms: int
    outcome: str
    parameter_count: int


@dataclass(frozen=True)
class BusinessRecord:
    """Public business-record envelope with already-projected fields."""

    backend: str
    record_type: str
    record_id: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceType": "timeseries" if self.backend == TIMESERIES else "business",
            "recordType": self.record_type,
            "recordId": self.record_id,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True)
class BusinessQueryResult:
    backend: str
    operation: str
    records: tuple[BusinessRecord, ...]
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "operation": self.operation,
            "records": [record.to_dict() for record in self.records],
            "truncated": self.truncated,
        }


class BusinessQueryAdapter(Protocol):
    """Stable application boundary for either approved business backend."""

    async def execute(
        self,
        request: BusinessQueryRequest | Mapping[str, Any],
        scope: BusinessAclScope,
    ) -> BusinessQueryResult:
        ...


@dataclass(frozen=True)
class _QuerySpec:
    backend: BackendName
    operation: str
    sql: str
    output_fields: tuple[str, ...]
    record_id_field: str
    max_limit: int = 100
    max_range: timedelta | None = None


# SQL is code-owned and contains no user-controlled identifiers.  The table
# and field names are provisional until the customer schema contract exists.
_QUERY_SPECS: dict[tuple[str, str], _QuerySpec] = {
    (POSTGRESQL, "get_equipment_summary"): _QuerySpec(
        backend=POSTGRESQL,
        operation="get_equipment_summary",
        sql=(
            "SELECT tenant_id, equipment_id, department_id, equipment_name, "
            "model, status, updated_at "
            "FROM business_equipment "
            "WHERE tenant_id = %s AND department_id = ANY(%s) "
            "AND equipment_id = %s LIMIT %s"
        ),
        output_fields=(
            "equipment_id",
            "department_id",
            "equipment_name",
            "model",
            "status",
            "updated_at",
        ),
        record_id_field="equipment_id",
        max_limit=1,
    ),
    (POSTGRESQL, "list_recent_repairs"): _QuerySpec(
        backend=POSTGRESQL,
        operation="list_recent_repairs",
        sql=(
            "SELECT tenant_id, repair_id, equipment_id, department_id, "
            "repair_status, fault_code, started_at, completed_at, summary "
            "FROM business_repair_record "
            "WHERE tenant_id = %s AND department_id = ANY(%s) "
            "AND equipment_id = %s "
            "ORDER BY COALESCE(completed_at, started_at) DESC LIMIT %s"
        ),
        output_fields=(
            "repair_id",
            "equipment_id",
            "department_id",
            "repair_status",
            "fault_code",
            "started_at",
            "completed_at",
            "summary",
        ),
        record_id_field="repair_id",
        max_limit=20,
    ),
    (POSTGRESQL, "list_recent_maintenance"): _QuerySpec(
        backend=POSTGRESQL,
        operation="list_recent_maintenance",
        sql=(
            "SELECT tenant_id, maintenance_id, equipment_id, department_id, "
            "maintenance_type, maintenance_status, performed_at, due_at, summary "
            "FROM business_maintenance_record "
            "WHERE tenant_id = %s AND department_id = ANY(%s) "
            "AND equipment_id = %s "
            "ORDER BY performed_at DESC LIMIT %s"
        ),
        output_fields=(
            "maintenance_id",
            "equipment_id",
            "department_id",
            "maintenance_type",
            "maintenance_status",
            "performed_at",
            "due_at",
            "summary",
        ),
        record_id_field="maintenance_id",
        max_limit=20,
    ),
    (POSTGRESQL, "get_fault_history"): _QuerySpec(
        backend=POSTGRESQL,
        operation="get_fault_history",
        sql=(
            "SELECT tenant_id, fault_id, equipment_id, department_id, fault_code, "
            "occurred_at, cleared_at, status, summary "
            "FROM business_fault_history "
            "WHERE tenant_id = %s AND department_id = ANY(%s) "
            "AND equipment_id = %s AND fault_code = %s "
            "AND occurred_at >= %s AND occurred_at < %s "
            "ORDER BY occurred_at DESC LIMIT %s"
        ),
        output_fields=(
            "fault_id",
            "equipment_id",
            "department_id",
            "fault_code",
            "occurred_at",
            "cleared_at",
            "status",
            "summary",
        ),
        record_id_field="fault_id",
        max_limit=100,
        max_range=timedelta(days=31),
    ),
    (TIMESERIES, "list_measurements"): _QuerySpec(
        backend=TIMESERIES,
        operation="list_measurements",
        sql=(
            "SELECT tenant_id, measurement_id, equipment_id, department_id, "
            "metric, value, unit, measured_at "
            "FROM business_timeseries_measurement "
            "WHERE tenant_id = %s AND department_id = ANY(%s) "
            "AND equipment_id = %s AND metric = %s "
            "AND measured_at >= %s AND measured_at < %s "
            "ORDER BY measured_at DESC LIMIT %s"
        ),
        output_fields=(
            "measurement_id",
            "equipment_id",
            "department_id",
            "metric",
            "value",
            "unit",
            "measured_at",
        ),
        record_id_field="measurement_id",
        max_limit=100,
        max_range=timedelta(hours=24),
    ),
}


_FORBIDDEN_SQL = re.compile(
    r"\b(?:insert|update|delete|merge|truncate|alter|drop|create|grant|revoke|"
    r"copy|call|do|execute|listen|notify|set|reset|vacuum|analyze|for\s+update)\b",
    re.IGNORECASE,
)


def _validate_readonly_sql(sql: str) -> None:
    """Defend the transport boundary even though SQL comes from this module."""

    normalized = sql.strip()
    if not re.match(r"^select\b", normalized, re.IGNORECASE):
        raise BusinessQueryConfigurationError(
            "BUSINESS_QUERY_SQL_NOT_READONLY",
            "Configured business query is not read-only",
        )
    if ";" in normalized or "--" in normalized or "/*" in normalized:
        raise BusinessQueryConfigurationError(
            "BUSINESS_QUERY_SQL_NOT_READONLY",
            "Configured business query contains unsafe SQL syntax",
        )
    if _FORBIDDEN_SQL.search(normalized):
        raise BusinessQueryConfigurationError(
            "BUSINESS_QUERY_SQL_NOT_READONLY",
            "Configured business query contains a write operation",
        )


class UnconfiguredQueryTransport:
    """Fail-closed transport used until a real driver contract is supplied."""

    def __init__(self, backend: str) -> None:
        self.backend = backend

    async def fetch(
        self,
        *,
        sql: str,
        params: tuple[Any, ...],
        timeout_seconds: float,
        readonly: bool,
    ) -> Sequence[Mapping[str, Any]]:
        raise BusinessQueryUnavailableError(
            "BUSINESS_QUERY_TRANSPORT_UNCONFIGURED",
            f"No read-only {self.backend} transport is configured",
        )


class ReadonlyBusinessQueryAdapter:
    """Execute only code-owned, tenant-scoped read operations."""

    def __init__(
        self,
        transports: Mapping[str, ReadonlyQueryTransport],
        *,
        timeout_seconds: float = 5.0,
        timeouts: Mapping[str, float] | None = None,
        max_rows: int = 100,
        max_range: timedelta = timedelta(days=31),
        timeseries_max_range: timedelta = timedelta(hours=24),
        audit_sink: QueryAuditSink | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise BusinessQueryConfigurationError(
                "BUSINESS_QUERY_TIMEOUT_INVALID",
                "Business query timeout must be between 0 and 60 seconds",
            )
        if not isinstance(max_rows, int) or not 1 <= max_rows <= 500:
            raise BusinessQueryConfigurationError(
                "BUSINESS_QUERY_ROW_LIMIT_INVALID",
                "Business query row limit is outside the safe range",
            )
        if max_range <= timedelta(0) or timeseries_max_range <= timedelta(0):
            raise BusinessQueryConfigurationError(
                "BUSINESS_QUERY_RANGE_INVALID",
                "Business query time range must be positive",
            )
        self.transports = dict(transports)
        self.timeout_seconds = timeout_seconds
        self.timeouts = dict(timeouts or {})
        self.max_rows = max_rows
        self.max_range = max_range
        self.timeseries_max_range = timeseries_max_range
        self.audit_sink = audit_sink or NoopQueryAuditSink()

    def _timeout_for(self, backend: str) -> float:
        timeout = self.timeouts.get(backend, self.timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise BusinessQueryConfigurationError(
                "BUSINESS_QUERY_TIMEOUT_INVALID",
                "Business query timeout must be between 0 and 60 seconds",
            )
        return timeout

    async def execute(
        self,
        request: BusinessQueryRequest | Mapping[str, Any],
        scope: BusinessAclScope,
    ) -> BusinessQueryResult:
        if not isinstance(request, BusinessQueryRequest):
            try:
                request = BusinessQueryRequest.model_validate(request)
            except Exception as exc:
                raise BusinessQueryValidationError(
                    "BUSINESS_QUERY_REQUEST_INVALID",
                    "Business query request is invalid",
                ) from exc
        if not isinstance(scope, BusinessAclScope) or scope.is_empty:
            raise BusinessQueryPermissionError(
                "BUSINESS_QUERY_SCOPE_DENIED",
                "Business query scope is empty",
            )
        if not scope.permits(request.equipment_id):
            raise BusinessQueryPermissionError(
                "BUSINESS_QUERY_SCOPE_DENIED",
                "Equipment is outside the authorized business scope",
            )

        spec = _QUERY_SPECS.get((request.backend, request.operation))
        if spec is None:
            raise BusinessQueryValidationError(
                "BUSINESS_QUERY_OPERATION_NOT_ALLOWED",
                "Business query operation is not allowed",
            )
        effective_limit = min(request.limit, self.max_rows, spec.max_limit)
        self._validate_range(request, spec)
        sql = spec.sql
        _validate_readonly_sql(sql)
        params = self._parameters(request, scope, effective_limit)
        transport = self.transports.get(request.backend)
        if transport is None:
            transport = UnconfiguredQueryTransport(request.backend)
        timeout_seconds = self._timeout_for(request.backend)
        started = time.monotonic()
        outcome = "failed"
        row_count = 0
        try:
            rows = await asyncio.wait_for(
                transport.fetch(
                    sql=sql,
                    params=params,
                    timeout_seconds=timeout_seconds,
                    readonly=True,
                ),
                timeout=timeout_seconds,
            )
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise BusinessQueryResultError(
                    "BUSINESS_QUERY_RESULT_INVALID",
                    "Business query transport returned an invalid result",
                )
            row_count = len(rows)
            records = tuple(
                self._record_from_row(row, request, scope, spec)
                for row in rows[:effective_limit]
            )
            outcome = "completed"
            return BusinessQueryResult(
                backend=request.backend,
                operation=request.operation,
                records=records,
                truncated=len(rows) > effective_limit,
            )
        except asyncio.TimeoutError as exc:
            raise BusinessQueryTimeoutError(
                "BUSINESS_QUERY_TIMEOUT",
                "Business query exceeded its configured timeout",
            ) from exc
        except BusinessQueryError:
            raise
        except Exception as exc:
            raise BusinessQueryUnavailableError(
                "BUSINESS_QUERY_TRANSPORT_FAILED",
                "Business query transport failed",
            ) from exc
        finally:
            await self._audit(
                request=request,
                scope=scope,
                row_count=row_count,
                started=started,
                outcome=outcome,
            )

    def _validate_range(self, request: BusinessQueryRequest, spec: _QuerySpec) -> None:
        if request.from_ is None or request.to is None:
            return
        maximum = (
            self.timeseries_max_range
            if request.backend == TIMESERIES
            else self.max_range
        )
        if spec.max_range is not None:
            maximum = min(maximum, spec.max_range)
        start = request.from_.astimezone(timezone.utc)
        end = request.to.astimezone(timezone.utc)
        if end - start > maximum:
            raise BusinessQueryValidationError(
                "BUSINESS_QUERY_RANGE_TOO_LARGE",
                "Business query time range exceeds the configured maximum",
            )

    @staticmethod
    def _parameters(
        request: BusinessQueryRequest,
        scope: BusinessAclScope,
        limit: int,
    ) -> tuple[Any, ...]:
        common: tuple[Any, ...] = (
            scope.tenant_id,
            list(scope.department_ids),
            request.equipment_id,
        )
        if request.operation == "get_equipment_summary":
            return common + (limit,)
        if request.operation in {"list_recent_repairs", "list_recent_maintenance"}:
            return common + (limit,)
        if request.operation == "get_fault_history":
            return common + (
                request.fault_code,
                request.from_.astimezone(timezone.utc),
                request.to.astimezone(timezone.utc),
                limit,
            )
        if request.operation == "list_measurements":
            return common + (
                request.metric,
                request.from_.astimezone(timezone.utc),
                request.to.astimezone(timezone.utc),
                limit,
            )
        raise BusinessQueryValidationError(
            "BUSINESS_QUERY_OPERATION_NOT_ALLOWED",
            "Business query operation is not allowed",
        )

    @staticmethod
    def _record_from_row(
        row: Mapping[str, Any],
        request: BusinessQueryRequest,
        scope: BusinessAclScope,
        spec: _QuerySpec,
    ) -> BusinessRecord:
        if not isinstance(row, Mapping):
            raise BusinessQueryResultError(
                "BUSINESS_QUERY_RESULT_INVALID",
                "Business query row is not an object",
            )
        if row.get("tenant_id") != scope.tenant_id:
            raise BusinessQueryScopeViolation(
                "BUSINESS_QUERY_SCOPE_VIOLATION",
                "Business query returned a different tenant",
            )
        if row.get("department_id") not in scope.department_ids:
            raise BusinessQueryScopeViolation(
                "BUSINESS_QUERY_SCOPE_VIOLATION",
                "Business query returned a different department",
            )
        if row.get("equipment_id") != request.equipment_id:
            raise BusinessQueryScopeViolation(
                "BUSINESS_QUERY_SCOPE_VIOLATION",
                "Business query returned a different equipment",
            )
        if (
            request.operation == "get_fault_history"
            and row.get("fault_code") != request.fault_code
        ):
            raise BusinessQueryScopeViolation(
                "BUSINESS_QUERY_SCOPE_VIOLATION",
                "Business query returned a different fault code",
            )
        if (
            request.operation == "list_measurements"
            and row.get("metric") != request.metric
        ):
            raise BusinessQueryScopeViolation(
                "BUSINESS_QUERY_SCOPE_VIOLATION",
                "Business query returned a different metric",
            )
        record_id = row.get(spec.record_id_field)
        if not isinstance(record_id, (str, int)) or not str(record_id):
            raise BusinessQueryResultError(
                "BUSINESS_QUERY_RESULT_INVALID",
                "Business query row has no stable record id",
            )
        projected = {
            field_name: row[field_name]
            for field_name in spec.output_fields
            if field_name in row
        }
        return BusinessRecord(
            backend=request.backend,
            record_type=request.operation,
            record_id=str(record_id),
            fields=projected,
        )

    async def _audit(
        self,
        *,
        request: BusinessQueryRequest,
        scope: BusinessAclScope,
        row_count: int,
        started: float,
        outcome: str,
    ) -> None:
        event = BusinessQueryAuditEvent(
            backend=request.backend,
            operation=request.operation,
            tenant_id=scope.tenant_id,
            requested_limit=request.limit,
            row_count=row_count,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            outcome=outcome,
            parameter_count=(
                7
                if request.operation in {"get_fault_history", "list_measurements"}
                else 4
            ),
        )
        try:
            await self.audit_sink.record(event)
        except Exception:
            logger.warning("Business query audit sink failed")


class BusinessQueryService:
    """Bind a request to the authenticated principal before adapter execution."""

    def __init__(
        self,
        adapter: BusinessQueryAdapter,
        scope_resolver: BusinessScopeResolver | None,
    ) -> None:
        self.adapter = adapter
        self.scope_resolver = scope_resolver

    async def execute(
        self,
        context: AclContext | None,
        request: BusinessQueryRequest | Mapping[str, Any],
    ) -> BusinessQueryResult:
        if not isinstance(request, BusinessQueryRequest):
            try:
                request = BusinessQueryRequest.model_validate(request)
            except Exception as exc:
                raise BusinessQueryValidationError(
                    "BUSINESS_QUERY_REQUEST_INVALID",
                    "Business query request is invalid",
                ) from exc
        scope = await compile_business_scope(
            context,
            request.equipment_id,
            self.scope_resolver,
        )
        if scope.is_empty:
            raise BusinessQueryPermissionError(
                "BUSINESS_QUERY_SCOPE_DENIED",
                "Business query scope is empty",
            )
        return await self.adapter.execute(request, scope)


class BusinessPostgresAdapter:
    """PostgreSQL-only view over the generic read-only adapter."""

    def __init__(
        self,
        transport: ReadonlyQueryTransport,
        **kwargs: Any,
    ) -> None:
        self._adapter = ReadonlyBusinessQueryAdapter(
            {POSTGRESQL: transport}, **kwargs
        )

    async def execute(
        self,
        request: BusinessQueryRequest | Mapping[str, Any],
        scope: BusinessAclScope,
    ) -> BusinessQueryResult:
        return await self._adapter.execute(request, scope)


class BusinessTimeSeriesAdapter:
    """Time-series-only view over the generic read-only adapter."""

    def __init__(
        self,
        transport: ReadonlyQueryTransport,
        **kwargs: Any,
    ) -> None:
        self._adapter = ReadonlyBusinessQueryAdapter(
            {TIMESERIES: transport}, **kwargs
        )

    async def execute(
        self,
        request: BusinessQueryRequest | Mapping[str, Any],
        scope: BusinessAclScope,
    ) -> BusinessQueryResult:
        return await self._adapter.execute(request, scope)


def build_business_query_adapter(
    gateway_config: Any,
    *,
    postgres_transport: ReadonlyQueryTransport | None = None,
    timeseries_transport: ReadonlyQueryTransport | None = None,
    audit_sink: QueryAuditSink | None = None,
) -> ReadonlyBusinessQueryAdapter:
    """Build the adapter without importing or inventing a database driver."""

    validator = getattr(gateway_config, "validate_business_query", None)
    if validator is None:
        raise BusinessQueryConfigurationError(
            "BUSINESS_QUERY_CONFIG_INVALID",
            "Gateway configuration does not expose business query validation",
        )
    try:
        validator()
    except BusinessQueryError:
        raise
    except Exception as exc:
        raise BusinessQueryConfigurationError(
            "BUSINESS_QUERY_CONFIG_INVALID",
            "Business query configuration is invalid",
        ) from exc

    transports: dict[str, ReadonlyQueryTransport] = {}
    if getattr(gateway_config, "business_query_enabled", False):
        transports[POSTGRESQL] = postgres_transport or UnconfiguredQueryTransport(
            POSTGRESQL
        )
    if getattr(gateway_config, "timeseries_query_enabled", False):
        transports[TIMESERIES] = timeseries_transport or UnconfiguredQueryTransport(
            TIMESERIES
        )
    return ReadonlyBusinessQueryAdapter(
        transports,
        timeout_seconds=getattr(gateway_config, "pg_timeout", 5.0),
        timeouts={
            POSTGRESQL: getattr(gateway_config, "pg_timeout", 5.0),
            TIMESERIES: getattr(gateway_config, "timeseries_timeout", 5.0),
        },
        max_rows=getattr(gateway_config, "business_query_max_rows", 100),
        max_range=timedelta(
            days=getattr(gateway_config, "business_query_max_range_days", 31)
        ),
        timeseries_max_range=timedelta(
            hours=getattr(gateway_config, "timeseries_query_max_range_hours", 24)
        ),
        audit_sink=audit_sink,
    )


__all__ = [
    "ALLOWED_METRICS",
    "BusinessAclScope",
    "BusinessPostgresAdapter",
    "BusinessQueryAdapter",
    "BusinessQueryAuditEvent",
    "BusinessQueryConfigurationError",
    "BusinessQueryError",
    "BusinessQueryPermissionError",
    "BusinessQueryRequest",
    "BusinessQueryResult",
    "BusinessQueryResultError",
    "BusinessQueryService",
    "BusinessQueryScopeViolation",
    "BusinessQueryTimeoutError",
    "BusinessQueryValidationError",
    "BusinessTimeSeriesAdapter",
    "BusinessRecord",
    "BusinessScopeResolver",
    "NoopQueryAuditSink",
    "POSTGRESQL",
    "PrincipalBusinessScopeResolver",
    "ReadonlyBusinessQueryAdapter",
    "ReadonlyQueryTransport",
    "TIMESERIES",
    "UnconfiguredQueryTransport",
    "build_business_query_adapter",
    "compile_business_scope",
]
