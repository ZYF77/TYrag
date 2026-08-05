"""AclContext — principal plus runtime facts for ACL compilation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from enterprise.gateway.acl.policy import ACL_POLICY_VERSION
from enterprise.gateway.auth.user_principal import UserPrincipal


@dataclass(frozen=True)
class AclContext:
    """Authenticated principal and the policy version active at request time.

    Runtime facts from the customer permission service and business PostgreSQL
    are added by later milestones; the model intentionally keeps only identity
    and policy metadata for M1.
    """

    principal: UserPrincipal
    policy_version: str = ACL_POLICY_VERSION
    compiled_at: float = field(default_factory=time.time)
