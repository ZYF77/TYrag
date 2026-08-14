"""WP-01 Phase 2 M1+M2 tests: ACL models, scope interface, frozen rules."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.acl.policy import ACL_POLICY_VERSION, evaluate_document_acl
from enterprise.gateway.acl.schema import (
    SCOPE_MODE_MATERIALIZED,
    SCOPE_MODE_METADATA_PREDICATE,
    AclScope,
    DocumentAclFacts,
)
from enterprise.gateway.acl.scope import compile_scope
from enterprise.gateway.auth.user_principal import UserPrincipal


def _principal(**kwargs) -> UserPrincipal:
    values = dict(
        tenant_id="t1",
        business_user_id="u1",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        token_issued_at=int(time.time()) - 60,
        token_expires_at=int(time.time()) + 3600,
    )
    values.update(kwargs)
    if "capabilities" not in kwargs:
        values["capabilities"] = UserPrincipal._derive_capabilities(
            values["role_codes"], values["security_level"]
        )
    return UserPrincipal(**values)


def _facts(**kwargs) -> DocumentAclFacts:
    values = dict(
        tenant_id="t1",
        department_id="d10",
        security_level=2,
        business_status="active",
        allow_group_ids=("maintenance",),
        deny_group_ids=(),
    )
    values.update(kwargs)
    return DocumentAclFacts(**values)


# -- acl-policy-examples.json --


def test_policy_version_is_1_1():
    assert ACL_POLICY_VERSION == "1.1"


def test_policy_example_department_and_security_allowed():
    decision = evaluate_document_acl(_principal(), _facts())
    assert decision.allowed is True
    assert decision.rule == "ALLOWED"


def test_policy_example_deny_wins():
    principal = _principal(
        department_ids=("d10",),
        group_ids=("maintenance", "contractor"),
        security_level=3,
    )
    facts = _facts(
        department_id="d10",
        security_level=1,
        allow_group_ids=("maintenance",),
        deny_group_ids=("contractor",),
    )
    decision = evaluate_document_acl(principal, facts)
    assert decision.allowed is False
    assert decision.rule == "DENY_GROUP_HIT"


def test_policy_example_disabled_document():
    principal = _principal(security_level=5)
    facts = _facts(security_level=1, business_status="disabled")
    decision = evaluate_document_acl(principal, facts)
    assert decision.allowed is False
    assert decision.rule == "DOCUMENT_STATUS_DENIED"


# -- frozen rules: tenant / status / deny-first / missing fields --


def test_tenant_mismatch_denied():
    decision = evaluate_document_acl(_principal(), _facts(tenant_id="t2"))
    assert decision.allowed is False
    assert decision.rule == "TENANT_MISMATCH"


def test_missing_document_tenant_denied():
    decision = evaluate_document_acl(_principal(), _facts(tenant_id=None))
    assert decision.allowed is False
    assert decision.rule == "TENANT_MISMATCH"


def test_inactive_principal_denied():
    principal = _principal(mapping_status="disabled")
    decision = evaluate_document_acl(principal, _facts())
    assert decision.allowed is False
    assert decision.rule == "PRINCIPAL_INVALID"


def test_expired_principal_denied():
    principal = _principal(token_expires_at=int(time.time()) - 10)
    decision = evaluate_document_acl(principal, _facts())
    assert decision.allowed is False
    assert decision.rule == "PRINCIPAL_INVALID"


def test_missing_principal_denied():
    decision = evaluate_document_acl(None, _facts())
    assert decision.allowed is False
    assert decision.rule == "PRINCIPAL_INVALID"


@pytest.mark.parametrize(
    "status",
    ["superseded", "review_required", "deleted", "disabled", ""],
)
def test_non_active_document_status_denied(status):
    decision = evaluate_document_acl(_principal(), _facts(business_status=status))
    assert decision.allowed is False
    assert decision.rule == "DOCUMENT_STATUS_DENIED"


def test_deny_group_wins_over_allow_group():
    principal = _principal(group_ids=("maintenance", "contractor"))
    facts = _facts(
        allow_group_ids=("maintenance",),
        deny_group_ids=("contractor",),
    )
    decision = evaluate_document_acl(principal, facts)
    assert decision.allowed is False
    assert decision.rule == "DENY_GROUP_HIT"


def test_allow_group_missing_denied():
    principal = _principal(group_ids=("electrician",))
    decision = evaluate_document_acl(principal, _facts())
    assert decision.allowed is False
    assert decision.rule == "ALLOW_GROUP_MISSING"


def test_empty_allow_groups_unresolved():
    decision = evaluate_document_acl(_principal(), _facts(allow_group_ids=()))
    assert decision.allowed is False
    assert decision.rule == "UNRESOLVED"


def test_missing_user_department_is_allowed():
    principal = _principal(department_ids=())
    decision = evaluate_document_acl(principal, _facts())
    assert decision.allowed is True
    assert decision.rule == "ALLOWED"


def test_document_department_mismatch_is_allowed():
    decision = evaluate_document_acl(_principal(), _facts(department_id="d99"))
    assert decision.allowed is True
    assert decision.rule == "ALLOWED"


def test_missing_document_department_is_allowed():
    decision = evaluate_document_acl(_principal(), _facts(department_id=None))
    assert decision.allowed is True
    assert decision.rule == "ALLOWED"


def test_security_level_insufficient_denied():
    principal = _principal(security_level=1)
    decision = evaluate_document_acl(principal, _facts(security_level=2))
    assert decision.allowed is False
    assert decision.rule == "SECURITY_LEVEL_DENIED"


def test_missing_document_security_level_unresolved():
    decision = evaluate_document_acl(_principal(), _facts(security_level=None))
    assert decision.allowed is False
    assert decision.rule == "UNRESOLVED"


def test_admin_does_not_bypass_by_default():
    principal = _principal(role_codes=("system_admin",), security_level=0)
    decision = evaluate_document_acl(principal, _facts(security_level=5))
    assert decision.allowed is False


# -- M1 scope model and compile_scope interface --


def test_empty_scope_is_empty():
    scope = AclScope.empty(policy_version="1")
    assert scope.is_empty is True


def test_materialized_scope_not_empty():
    scope = AclScope.materialized(["ds1"], ["doc1"], policy_version="1")
    assert scope.is_empty is False
    assert scope.scope_mode == SCOPE_MODE_MATERIALIZED
    assert scope.document_ids == ("doc1",)


def test_materialized_scope_empty_documents_is_empty():
    scope = AclScope.materialized(["ds1"], [], policy_version="1")
    assert scope.is_empty is True


def test_metadata_predicate_scope_not_empty():
    scope = AclScope.metadata_predicate(
        ["ds1"],
        {"method": "manual", "logic": "and", "manual": [
            {"key": "tenant_id", "op": "=", "value": "t1"},
        ]},
        policy_version="1",
    )
    assert scope.is_empty is False
    assert scope.scope_mode == SCOPE_MODE_METADATA_PREDICATE


def test_metadata_predicate_empty_manual_is_empty():
    scope = AclScope.metadata_predicate(
        ["ds1"],
        {"method": "manual", "logic": "and", "manual": []},
        policy_version="1",
    )
    assert scope.is_empty is True


def test_metadata_predicate_empty_condition_dict_is_empty():
    scope = AclScope.metadata_predicate(
        ["ds1"],
        {"method": "manual", "logic": "and", "manual": [{}]},
        policy_version="1",
    )
    assert scope.is_empty is True


def test_metadata_predicate_incomplete_condition_is_empty():
    scope = AclScope.metadata_predicate(
        ["ds1"],
        {"method": "manual", "logic": "and", "manual": [
            {"key": "tenant_id", "op": "="},
        ]},
        policy_version="1",
    )
    assert scope.is_empty is True


def test_scope_metadata_filter_empty_dict_is_empty():
    scope = AclScope(dataset_ids=(), metadata_filter={}, policy_version="1")
    assert scope.is_empty is True


def test_compile_scope_without_resolver_is_empty():
    scope = asyncio.run(compile_scope(AclContext(principal=_principal())))
    assert isinstance(scope, AclScope)
    assert scope.is_empty is True


def test_compile_scope_inactive_principal_is_empty():
    principal = _principal(mapping_status="disabled")
    scope = asyncio.run(compile_scope(AclContext(principal=principal)))
    assert scope.is_empty is True


def test_compile_scope_uses_resolver():
    class Resolver:
        async def resolve(self, context):
            return AclScope.materialized(
                ["ds1"], ["doc1"], policy_version=context.policy_version
            )

    scope = asyncio.run(
        compile_scope(AclContext(principal=_principal()), resolver=Resolver())
    )
    assert scope.document_ids == ("doc1",)
    assert scope.is_empty is False


def test_compile_scope_fails_closed_when_resolver_raises():
    async def resolver(context):
        raise RuntimeError("resolver unavailable")

    scope = asyncio.run(compile_scope(AclContext(principal=_principal()), resolver=resolver))
    assert scope.is_empty is True


def test_compile_scope_rejects_non_acl_scope():
    async def resolver(context):
        return {"dataset_ids": ["ds1"]}

    scope = asyncio.run(compile_scope(AclContext(principal=_principal()), resolver=resolver))
    assert scope.is_empty is True


def test_compile_scope_rejects_materialized_without_documents():
    async def resolver(context):
        return AclScope(
            dataset_ids=("ds1",),
            document_ids=(),
            scope_mode=SCOPE_MODE_MATERIALIZED,
            policy_version=context.policy_version,
        )

    scope = asyncio.run(compile_scope(AclContext(principal=_principal()), resolver=resolver))
    assert scope.is_empty is True


def test_compile_scope_rejects_metadata_predicate_without_manual():
    async def resolver(context):
        return AclScope(
            dataset_ids=("ds1",),
            metadata_filter={"method": "manual", "logic": "and", "manual": []},
            scope_mode=SCOPE_MODE_METADATA_PREDICATE,
            policy_version=context.policy_version,
        )

    scope = asyncio.run(compile_scope(AclContext(principal=_principal()), resolver=resolver))
    assert scope.is_empty is True


def test_compile_scope_rejects_empty_manual_condition_dict():
    async def resolver(context):
        return AclScope.metadata_predicate(
            ["ds1"],
            {"method": "manual", "logic": "and", "manual": [{}]},
            policy_version=context.policy_version,
        )

    scope = asyncio.run(compile_scope(AclContext(principal=_principal()), resolver=resolver))
    assert scope.is_empty is True


def test_unresolved_decision_is_not_allowed():
    decision = evaluate_document_acl(_principal(), _facts(allow_group_ids=()))
    assert decision.allowed is False
    assert decision.rule == "UNRESOLVED"
