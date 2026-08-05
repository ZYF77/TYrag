"""Frozen ACL safety rules for document access (WP-01 Phase 2 M2).

Only rules marked FROZEN in the ACL Design Freeze are implemented as
enforceable decisions. Rules marked PENDING are implemented with fail-closed
defaults; their business semantics must be confirmed by the customer before
the defaults are changed. Tenant, status and deny-group checks are always
applied and are never relaxed by the pending switches.
"""
from __future__ import annotations

from dataclasses import dataclass

from enterprise.gateway.acl.schema import AclDecision, DocumentAclFacts
from enterprise.gateway.auth.user_principal import UserPrincipal

ACL_POLICY_VERSION = "1"


@dataclass(frozen=True)
class AclPolicyConfig:
    """Switches for PENDING rules; defaults are fail-closed.

    - admin_bypass_document_acl: PENDING. Default False means system_admin
      does not bypass document ACL.
    - empty_allow_groups_public: PENDING. Default False means a document with
      no allow_group_ids has no allow rule and is denied.
    """

    admin_bypass_document_acl: bool = False
    empty_allow_groups_public: bool = False


def _deny(rule: str, reason: str) -> AclDecision:
    return AclDecision(allowed=False, rule=rule, reason=reason)


def evaluate_document_acl(
    principal: UserPrincipal | None,
    facts: DocumentAclFacts | None,
    config: AclPolicyConfig | None = None,
) -> AclDecision:
    """Evaluate the frozen document ACL rules, deny-first.

    Returns an AclDecision; callers must treat any non-allowed result as deny.
    """
    config = config or AclPolicyConfig()

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

    bypass = config.admin_bypass_document_acl and "admin" in principal.capabilities
    if not bypass:
        if not facts.department_id or facts.department_id not in principal.department_ids:
            return _deny("DEPARTMENT_DENIED", "document department is not granted")
        if facts.security_level is None or principal.security_level < facts.security_level:
            return _deny("SECURITY_LEVEL_DENIED", "principal security level is insufficient")
        allow_hit = bool(set(principal.group_ids) & set(facts.allow_group_ids))
        if facts.allow_group_ids:
            if not allow_hit:
                return _deny("ALLOW_GROUP_MISSING", "no allow group matched")
        elif not config.empty_allow_groups_public:
            return _deny("NO_ALLOW_RULE", "document has no allow rule and is not public")

    return AclDecision(
        allowed=True,
        rule="ALLOWED",
        reason="all frozen ACL rules satisfied",
    )
