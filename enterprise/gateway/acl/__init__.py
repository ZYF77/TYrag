"""ACL policy compilation (WP-01 Phase 2 M1+M2)."""
from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.acl.errors import AclDeniedError, AclError
from enterprise.gateway.acl.policy import (
    ACL_POLICY_VERSION,
    evaluate_document_acl,
)
from enterprise.gateway.acl.schema import (
    SCOPE_MODE_MATERIALIZED,
    SCOPE_MODE_METADATA_PREDICATE,
    AclDecision,
    AclScope,
    DocumentAclFacts,
)
from enterprise.gateway.acl.scope import ScopeResolver, compile_scope

__all__ = [
    "ACL_POLICY_VERSION",
    "AclContext",
    "AclDecision",
    "AclDeniedError",
    "AclError",
    "AclScope",
    "DocumentAclFacts",
    "SCOPE_MODE_MATERIALIZED",
    "SCOPE_MODE_METADATA_PREDICATE",
    "ScopeResolver",
    "compile_scope",
    "evaluate_document_acl",
]
