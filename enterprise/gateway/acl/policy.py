"""Temporary tenant-open document policy for the integration test stage."""
from __future__ import annotations

from enterprise.gateway.acl.schema import AclDecision, DocumentAclFacts
from enterprise.gateway.auth.user_principal import UserPrincipal

ACL_POLICY_VERSION = "test-tenant-open-1"


def _deny(rule: str, reason: str) -> AclDecision:
    return AclDecision(allowed=False, rule=rule, reason=reason)


def evaluate_document_acl(
    principal: UserPrincipal | None,
    facts: DocumentAclFacts | None,
) -> AclDecision:
    """Allow active same-tenant documents; role ACL is intentionally pending."""
    if principal is None or not principal.is_active or not principal.tenant_id:
        return _deny("PRINCIPAL_INVALID", "principal is missing, inactive or has no tenant")

    if facts is None or not facts.tenant_id:
        return _deny("TENANT_MISMATCH", "document has no tenant")
    if facts.tenant_id != principal.tenant_id:
        return _deny("TENANT_MISMATCH", "document tenant does not match principal tenant")

    if facts.business_status != "active":
        return _deny("DOCUMENT_STATUS_DENIED", "only active documents are retrievable")

    return AclDecision(
        allowed=True,
        rule="TEST_TENANT_OPEN",
        reason="test stage allows active documents within the same tenant",
    )
