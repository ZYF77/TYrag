"""Frozen ACL safety rules for document access (WP-01 Phase 2 M2).

Only rules marked FROZEN in the ACL Design Freeze are implemented as
enforceable decisions. PENDING inputs (missing department/security/allow
rule) produce UNRESOLVED, which callers must treat as deny. Tenant, status
and deny-group checks are always applied and are never relaxed.
"""
from __future__ import annotations

from enterprise.gateway.acl.schema import AclDecision, DocumentAclFacts
from enterprise.gateway.auth.user_principal import UserPrincipal

ACL_POLICY_VERSION = "1"


def _deny(rule: str, reason: str) -> AclDecision:
    return AclDecision(allowed=False, rule=rule, reason=reason)


def evaluate_document_acl(
    principal: UserPrincipal | None,
    facts: DocumentAclFacts | None,
) -> AclDecision:
    """Evaluate the frozen document ACL rules, deny-first.

    Returns an AclDecision; callers must treat any non-allowed result,
    including UNRESOLVED, as deny.
    """
    if principal is None or not principal.is_active or not principal.tenant_id:
        return _deny("PRINCIPAL_INVALID", "principal is missing, inactive or has no tenant")

    if facts is None or not facts.tenant_id:
        return _deny("TENANT_MISMATCH", "document has no tenant")
    if facts.tenant_id != principal.tenant_id:
        return _deny("TENANT_MISMATCH", "document tenant does not match principal tenant")

    if facts.business_status != "active":
        return _deny("DOCUMENT_STATUS_DENIED", "only active documents are retrievable")

    deny_hit = bool(set(principal.group_ids) & set(facts.deny_group_ids))
    if deny_hit:
        return _deny("DENY_GROUP_HIT", "deny group matched")

    if not facts.department_id or not principal.department_ids:
        return _deny("UNRESOLVED", "department rule has no usable input")
    if facts.department_id not in principal.department_ids:
        return _deny("DEPARTMENT_DENIED", "document department is not granted")
    if facts.security_level is None:
        return _deny("UNRESOLVED", "document security level is not set")
    if principal.security_level < facts.security_level:
        return _deny("SECURITY_LEVEL_DENIED", "principal security level is insufficient")
    if not facts.allow_group_ids:
        return _deny("UNRESOLVED", "document has no allow rule")
    if not (set(principal.group_ids) & set(facts.allow_group_ids)):
        return _deny("ALLOW_GROUP_MISSING", "no allow group matched")

    return AclDecision(
        allowed=True,
        rule="ALLOWED",
        reason="all frozen ACL rules satisfied",
    )
