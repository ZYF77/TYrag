"""Unit and adapter-contract tests for the read-only business query boundary.

The recording transport is a deterministic driver test double.  These tests
do not claim PostgreSQL/TimeSeries Integration acceptance; M2 remains blocked
until the customer schema, credentials and real transport are available.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import GatewayConfig
from enterprise.gateway.query.business_adapter import (
    BusinessAclScope,
    BusinessQueryAuditEvent,
    BusinessQueryPermissionError,
    BusinessQueryRequest,
    BusinessQueryScopeViolation,
    BusinessQueryService,
    BusinessQueryTimeoutError,
    PrincipalBusinessScopeResolver,
    ReadonlyBusinessQueryAdapter,
    UnconfiguredQueryTransport,
    build_business_query_adapter,
    compile_business_scope,
)


class RecordingTransport:
    def __init__(self, rows=None, *, delay: float = 0.0):
        self.rows = list(rows or [])
        self.delay = delay
        self.calls: list[dict] = []

    async def fetch(self, *, sql, params, timeout_seconds, readonly):
        self.calls.append(
            {
                "sql": sql,
                "params": params,
                "timeout_seconds": timeout_seconds,
                "readonly": readonly,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.rows


class RecordingAuditSink:
    def __init__(self):
        self.events: list[BusinessQueryAuditEvent] = []

    async def record(self, event):
        self.events.append(event)


def _principal(
    *,
    tenant_id: str = "tenant-a",
    departments: tuple[str, ...] = ("dept-maintenance",),
    mapping_status: str = "active",
) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant_id,
        business_user_id="user-a",
        subject="subject-a",
        department_ids=departments,
        group_ids=("maintenance",),
        security_level=2,
        mapping_status=mapping_status,
        capabilities=("read",),
    )


def _scope(
    *,
    tenant_id: str = "tenant-a",
    departments: tuple[str, ...] = ("dept-maintenance",),
    equipment: tuple[str, ...] = ("EQ-1",),
) -> BusinessAclScope:
    return BusinessAclScope(
        tenant_id=tenant_id,
        department_ids=departments,
        equipment_ids=equipment,
        policy_version="1",
    )


def _adapter(transport, *, audit_sink=None, timeout=5.0, max_rows=100):
    return ReadonlyBusinessQueryAdapter(
        {"postgresql": transport, "timeseries": transport},
        timeout_seconds=timeout,
        max_rows=max_rows,
        audit_sink=audit_sink,
    )


@pytest.mark.asyncio
async def test_postgres_contract_is_parameterized_readonly_and_projects_fields():
    transport = RecordingTransport(
        [
            {
                "tenant_id": "tenant-a",
                "repair_id": "repair-1",
                "equipment_id": "EQ-1",
                "department_id": "dept-maintenance",
                "repair_status": "completed",
                "fault_code": "E-104",
                "started_at": "2026-08-10T08:00:00Z",
                "completed_at": "2026-08-10T10:00:00Z",
                "summary": "changed filter",
                "secret_token": "must-not-be-projected",
            }
        ]
    )
    audit = RecordingAuditSink()
    adapter = _adapter(transport, audit_sink=audit)

    result = await adapter.execute(
        BusinessQueryRequest(
            backend="postgresql",
            operation="list_recent_repairs",
            equipment_id="EQ-1",
            limit=20,
        ),
        _scope(),
    )

    assert result.to_dict() == {
        "backend": "postgresql",
        "operation": "list_recent_repairs",
        "records": [
            {
                "sourceType": "business",
                "recordType": "list_recent_repairs",
                "recordId": "repair-1",
                "fields": {
                    "repair_id": "repair-1",
                    "equipment_id": "EQ-1",
                    "department_id": "dept-maintenance",
                    "repair_status": "completed",
                    "fault_code": "E-104",
                    "started_at": "2026-08-10T08:00:00Z",
                    "completed_at": "2026-08-10T10:00:00Z",
                    "summary": "changed filter",
                },
            }
        ],
        "truncated": False,
    }
    call = transport.calls[0]
    assert call["readonly"] is True
    assert call["params"] == ("tenant-a", ["dept-maintenance"], "EQ-1", 20)
    assert call["sql"].lstrip().upper().startswith("SELECT")
    assert ";" not in call["sql"]
    assert "secret_token" not in result.to_dict().__repr__()
    assert audit.events[0].row_count == 1
    assert audit.events[0].parameter_count == 4


@pytest.mark.asyncio
async def test_timeseries_contract_has_metric_and_bounded_time_window():
    transport = RecordingTransport(
        [
            {
                "tenant_id": "tenant-a",
                "measurement_id": "measurement-1",
                "equipment_id": "EQ-1",
                "department_id": "dept-maintenance",
                "metric": "temperature",
                "value": 42.5,
                "unit": "C",
                "measured_at": datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
            }
        ]
    )
    adapter = _adapter(transport)
    start = datetime(2026, 8, 10, 8, tzinfo=timezone.utc)
    result = await adapter.execute(
        {
            "backend": "timeseries",
            "operation": "list_measurements",
            "equipment_id": "EQ-1",
            "metric": "temperature",
            "from": start.isoformat(),
            "to": (start + timedelta(hours=1)).isoformat(),
            "limit": 10,
        },
        _scope(),
    )

    assert result.records[0].record_id == "measurement-1"
    assert result.records[0].to_dict()["sourceType"] == "timeseries"
    assert transport.calls[0]["params"][0] == "tenant-a"
    assert transport.calls[0]["params"][3] == "temperature"
    assert transport.calls[0]["readonly"] is True


def test_request_contract_rejects_sql_tenant_and_unapproved_fields():
    with pytest.raises(Exception):
        BusinessQueryRequest.model_validate(
            {
                "backend": "postgresql",
                "operation": "list_recent_repairs",
                "equipment_id": "EQ-1",
                "tenant_id": "tenant-b",
                "sql": "SELECT * FROM secrets",
            }
        )

    with pytest.raises(Exception):
        BusinessQueryRequest(
            backend="timeseries",
            operation="list_measurements",
            equipment_id="EQ-1",
            metric="arbitrary_column",
            from_=datetime(2026, 8, 10, tzinfo=timezone.utc),
            to=datetime(2026, 8, 10, 1, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_acl_scope_is_tenant_bound_and_resolver_fails_closed():
    context = AclContext(principal=_principal())
    scope = await compile_business_scope(
        context,
        "EQ-1",
        PrincipalBusinessScopeResolver(),
    )
    assert scope.tenant_id == "tenant-a"
    assert scope.equipment_ids == ("EQ-1",)

    assert (
        await compile_business_scope(context, "EQ-1", resolver=None)
    ).is_empty

    class CrossTenantResolver:
        async def resolve(self, context, equipment_id):
            return _scope(tenant_id="tenant-b")

    assert (
        await compile_business_scope(context, "EQ-1", CrossTenantResolver())
    ).is_empty

    inactive = await compile_business_scope(
        AclContext(principal=_principal(mapping_status="disabled")),
        "EQ-1",
        PrincipalBusinessScopeResolver(),
    )
    assert inactive.is_empty


@pytest.mark.asyncio
async def test_permission_negative_cases_never_reach_transport():
    transport = RecordingTransport([])
    adapter = _adapter(transport)
    service = BusinessQueryService(adapter, PrincipalBusinessScopeResolver())
    request = BusinessQueryRequest(
        backend="postgresql",
        operation="get_equipment_summary",
        equipment_id="EQ-1",
    )

    class RestrictedResolver:
        async def resolve(self, context, equipment_id):
            return _scope(
                tenant_id=context.principal.tenant_id,
                equipment=("EQ-2",),
            )

    with pytest.raises(BusinessQueryPermissionError):
        await BusinessQueryService(adapter, RestrictedResolver()).execute(
            AclContext(principal=_principal()), request
        )
    with pytest.raises(BusinessQueryPermissionError):
        await service.execute(
            AclContext(principal=_principal(mapping_status="disabled")), request
        )
    with pytest.raises(BusinessQueryPermissionError):
        await BusinessQueryService(adapter, None).execute(
            AclContext(principal=_principal()), request
        )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_transport_row_scope_violation_is_denied_not_filtered_after_query():
    transport = RecordingTransport(
        [
            {
                "tenant_id": "tenant-b",
                "equipment_id": "EQ-1",
                "department_id": "dept-maintenance",
                "equipment_name": "cross-tenant",
                "model": "x",
                "status": "active",
                "updated_at": "now",
            }
        ]
    )
    adapter = _adapter(transport)
    with pytest.raises(BusinessQueryScopeViolation):
        await adapter.execute(
            BusinessQueryRequest(
                backend="postgresql",
                operation="get_equipment_summary",
                equipment_id="EQ-1",
            ),
            _scope(),
        )


@pytest.mark.asyncio
async def test_timeout_is_enforced_and_audit_does_not_contain_row_content():
    transport = RecordingTransport(delay=0.05)
    audit = RecordingAuditSink()
    adapter = _adapter(transport, audit_sink=audit, timeout=0.001)
    with pytest.raises(BusinessQueryTimeoutError):
        await adapter.execute(
            BusinessQueryRequest(
                backend="postgresql",
                operation="list_recent_maintenance",
                equipment_id="EQ-1",
            ),
            _scope(),
        )
    assert audit.events[0].outcome == "failed"
    assert not hasattr(audit.events[0], "sql")


def test_config_validation_is_disabled_by_default_and_fail_closed_when_enabled(
    monkeypatch,
):
    cfg = GatewayConfig()
    cfg.validate_business_query()
    assert cfg.business_query_enabled is False

    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_TRANSPORT", "external")
    monkeypatch.setenv("PG_DATABASE", "business_db")
    monkeypatch.setenv("PG_USER", "readonly_user")
    enabled = GatewayConfig()
    enabled.validate_business_query()
    adapter = build_business_query_adapter(enabled)
    assert isinstance(adapter.transports["postgresql"], UnconfiguredQueryTransport)


def test_config_rejects_unfrozen_driver_and_unsafe_limits(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_TRANSPORT", "psycopg")
    with pytest.raises(ValueError):
        GatewayConfig().validate_business_query()

    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_TRANSPORT", "unconfigured")
    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_MAX_ROWS", "501")
    with pytest.raises(ValueError):
        GatewayConfig().validate_business_query()

    monkeypatch.setenv("ENTERPRISE_BUSINESS_QUERY_MAX_ROWS", "100")
    monkeypatch.setenv("PG_TIMEOUT", "61")
    with pytest.raises(ValueError):
        GatewayConfig().validate_business_query()


def test_adapter_factory_does_not_claim_a_missing_driver_is_available():
    cfg = GatewayConfig()
    adapter = build_business_query_adapter(cfg)
    assert adapter.transports == {}


@pytest.mark.asyncio
async def test_unconfigured_transport_reports_safe_unavailable_error():
    adapter = _adapter(UnconfiguredQueryTransport("postgresql"))
    with pytest.raises(Exception) as error:
        await adapter.execute(
            BusinessQueryRequest(
                backend="postgresql",
                operation="get_equipment_summary",
                equipment_id="EQ-1",
            ),
            _scope(),
        )
    assert getattr(error.value, "code", "") == "BUSINESS_QUERY_TRANSPORT_UNCONFIGURED"
