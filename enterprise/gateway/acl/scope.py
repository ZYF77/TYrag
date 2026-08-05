"""Scope compilation interface (WP-01 Phase 2 M1).

Materializing datasets, document sets and metadata predicates belongs to M3;
this module owns the AclScope contract and fail-closed behavior when a
resolver is unavailable or fails.
"""
from __future__ import annotations

import logging
from typing import Protocol

from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.acl.schema import AclScope

logger = logging.getLogger(__name__)


class ScopeResolver(Protocol):
    """Resolves a principal context into an allowed AclScope."""

    async def resolve(self, context: AclContext) -> AclScope:
        ...


async def compile_scope(
    context: AclContext,
    resolver: ScopeResolver | None = None,
) -> AclScope:
    """Compile the allowed scope, failing closed on any unsafe input."""
    if (
        context is None
        or context.principal is None
        or not context.principal.is_active
        or not context.principal.tenant_id
    ):
        return AclScope.empty(context.policy_version if context else "")

    if resolver is None:
        return AclScope.empty(context.policy_version)

    try:
        scope = await resolver.resolve(context)
    except Exception:
        logger.warning("ACL scope resolution failed; returning empty scope", exc_info=True)
        return AclScope.empty(context.policy_version)
    if scope is None:
        return AclScope.empty(context.policy_version)
    return scope
