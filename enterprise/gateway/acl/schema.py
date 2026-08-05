"""ACL data models: retrieval scope and document ACL facts.

WP-01 Phase 2 M1. The scope model supports two forms:
- materialized: an explicit allowed document set (only when the set is small);
- metadata predicate: a manual metadata filter compiled from the frozen rules.
An empty scope means no retrieval is allowed and callers must short-circuit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

ScopeMode = Literal["materialized", "metadata_predicate"]

SCOPE_MODE_MATERIALIZED: ScopeMode = "materialized"
SCOPE_MODE_METADATA_PREDICATE: ScopeMode = "metadata_predicate"


def has_manual_conditions(metadata_filter: dict[str, Any] | None) -> bool:
    """Return True only for a predicate with a non-empty manual condition list."""
    if not isinstance(metadata_filter, dict):
        return False
    if metadata_filter.get("method") != "manual":
        return False
    if metadata_filter.get("logic") not in ("and", "or"):
        return False
    manual = metadata_filter.get("manual")
    return (
        isinstance(manual, list)
        and len(manual) > 0
        and all(
            isinstance(item, dict)
            and isinstance(item.get("key"), str)
            and bool(item.get("key"))
            and isinstance(item.get("op"), str)
            and bool(item.get("op"))
            and "value" in item
            for item in manual
        )
    )


@dataclass(frozen=True)
class AclScope:
    """Allowed retrieval scope compiled by compile_scope().

    metadata_filter follows the RAGFlow manual meta_data_filter shape and is
    compiled in M3: {"method": "manual", "logic": "and|or", "manual": [...]}.
    """

    dataset_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    metadata_filter: dict[str, Any] | None = None
    scope_mode: ScopeMode = SCOPE_MODE_METADATA_PREDICATE
    policy_version: str = ""
    compiled_at: float = field(default_factory=time.time)

    @classmethod
    def materialized(
        cls,
        dataset_ids: tuple[str, ...] | list[str],
        document_ids: tuple[str, ...] | list[str],
        policy_version: str = "",
    ) -> "AclScope":
        if not document_ids:
            return cls.empty(policy_version)
        return cls(
            dataset_ids=tuple(dataset_ids),
            document_ids=tuple(document_ids),
            scope_mode=SCOPE_MODE_MATERIALIZED,
            policy_version=policy_version,
        )

    @classmethod
    def metadata_predicate(
        cls,
        dataset_ids: tuple[str, ...] | list[str],
        metadata_filter: dict[str, Any],
        policy_version: str = "",
    ) -> "AclScope":
        if not dataset_ids or not has_manual_conditions(metadata_filter):
            return cls.empty(policy_version)
        return cls(
            dataset_ids=tuple(dataset_ids),
            metadata_filter=metadata_filter,
            scope_mode=SCOPE_MODE_METADATA_PREDICATE,
            policy_version=policy_version,
        )

    @classmethod
    def empty(cls, policy_version: str = "") -> "AclScope":
        return cls(policy_version=policy_version)

    @property
    def is_empty(self) -> bool:
        """True when no retrieval can be performed with this scope."""
        if self.scope_mode == SCOPE_MODE_MATERIALIZED:
            return not self.document_ids
        if self.scope_mode == SCOPE_MODE_METADATA_PREDICATE:
            return not self.dataset_ids or not has_manual_conditions(self.metadata_filter)
        return True


@dataclass(frozen=True)
class DocumentAclFacts:
    """Document-level ACL facts sourced from document metadata (M2 input)."""

    tenant_id: str | None = None
    department_id: str | None = None
    equipment_id: str | None = None
    document_type: str | None = None
    security_level: int | None = None
    business_status: str = ""
    allow_group_ids: tuple[str, ...] = ()
    deny_group_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AclDecision:
    """Result of evaluating the frozen ACL rules for one resource."""

    allowed: bool
    rule: str
    reason: str
