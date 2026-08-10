"""Enterprise-owned dense+sparse hybrid fusion core.

This module deliberately has no HTTP or RAGFlow dependency.  Callers provide
ranked dense and sparse hits; the engine applies the frozen tenant/document
ACL before reciprocal-rank fusion, so unauthorized hits never enter the
fusion set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from enterprise.gateway.acl.policy import evaluate_document_acl
from enterprise.gateway.acl.schema import DocumentAclFacts
from enterprise.gateway.auth.user_principal import UserPrincipal


class HybridFusionError(ValueError):
    """Invalid fusion input or policy configuration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HybridAuthorizationError(HybridFusionError):
    """The caller cannot establish an active, tenant-scoped ACL context."""


@dataclass(frozen=True)
class HybridHit:
    """A ranked hit from one retrieval channel.

    ``acl_facts`` is required by the secure engine.  Missing facts fail closed
    instead of allowing a caller to fuse an unscoped retrieval result.
    """

    candidate_id: str
    tenant_id: str
    score: float
    payload: Mapping[str, Any] = field(default_factory=dict)
    acl_facts: DocumentAclFacts | None = None


@dataclass(frozen=True)
class HybridFusionPolicy:
    """Weighted reciprocal-rank fusion parameters."""

    dense_weight: float = 0.5
    sparse_weight: float = 0.5
    rrf_k: int = 60
    top_k: int = 20

    def __post_init__(self) -> None:
        for name, value in (
            ("dense_weight", self.dense_weight),
            ("sparse_weight", self.sparse_weight),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise HybridFusionError("FUSION_POLICY_INVALID", f"{name} must be finite")
            if value < 0:
                raise HybridFusionError("FUSION_POLICY_INVALID", f"{name} must be non-negative")
        if self.dense_weight == 0 and self.sparse_weight == 0:
            raise HybridFusionError(
                "FUSION_POLICY_INVALID",
                "at least one fusion channel must have positive weight",
            )
        if not isinstance(self.rrf_k, int) or self.rrf_k <= 0:
            raise HybridFusionError("FUSION_POLICY_INVALID", "rrf_k must be positive")
        if not isinstance(self.top_k, int) or not 1 <= self.top_k <= 100:
            raise HybridFusionError(
                "FUSION_POLICY_INVALID", "top_k must be between 1 and 100"
            )


@dataclass(frozen=True)
class FusionResult:
    """A fused, already-authorized candidate."""

    candidate_id: str
    tenant_id: str
    score: float
    dense_rank: int | None
    sparse_rank: int | None
    source_channels: tuple[str, ...]
    payload: Mapping[str, Any]


class HybridFusionEngine:
    """Filter retrieval channels with ACL, then apply deterministic weighted RRF."""

    def __init__(self, policy: HybridFusionPolicy | None = None) -> None:
        self.policy = policy or HybridFusionPolicy()

    def fuse(
        self,
        principal: UserPrincipal | None,
        dense_hits: Sequence[HybridHit],
        sparse_hits: Sequence[HybridHit],
        *,
        top_k: int | None = None,
    ) -> list[FusionResult]:
        if principal is None or not principal.is_active or not principal.tenant_id:
            raise HybridAuthorizationError(
                "FUSION_AUTHORIZATION_REQUIRED",
                "an active tenant-scoped principal is required",
            )
        requested_top_k = self.policy.top_k if top_k is None else top_k
        if not isinstance(requested_top_k, int) or not 1 <= requested_top_k <= 100:
            raise HybridFusionError(
                "FUSION_INPUT_INVALID", "top_k must be between 1 and 100"
            )

        # Authorization happens before rank maps are built.  This prevents a
        # cross-tenant or ACL-denied hit from influencing a fused score.
        dense = self._authorized_channel(principal, dense_hits)
        sparse = self._authorized_channel(principal, sparse_hits)
        return self._fuse_authorized(dense, sparse, requested_top_k)

    def _authorized_channel(
        self,
        principal: UserPrincipal,
        hits: Sequence[HybridHit],
    ) -> list[HybridHit]:
        authorized: list[HybridHit] = []
        for hit in hits:
            self._validate_hit(hit)
            if hit.tenant_id != principal.tenant_id:
                continue
            if hit.acl_facts is None:
                continue
            if not isinstance(hit.acl_facts, DocumentAclFacts):
                raise HybridFusionError(
                    "FUSION_INPUT_INVALID", "acl_facts must be DocumentAclFacts"
                )
            decision = evaluate_document_acl(principal, hit.acl_facts)
            if decision.allowed:
                authorized.append(hit)
        return authorized

    @staticmethod
    def _validate_hit(hit: HybridHit) -> None:
        if not isinstance(hit, HybridHit):
            raise HybridFusionError("FUSION_INPUT_INVALID", "all hits must be HybridHit")
        if not isinstance(hit.candidate_id, str) or not hit.candidate_id.strip():
            raise HybridFusionError("FUSION_INPUT_INVALID", "candidate_id is required")
        if len(hit.candidate_id) > 256:
            raise HybridFusionError("FUSION_INPUT_INVALID", "candidate_id is too long")
        if not isinstance(hit.tenant_id, str) or not hit.tenant_id.strip():
            raise HybridFusionError("FUSION_INPUT_INVALID", "tenant_id is required")
        if not isinstance(hit.score, (int, float)) or not math.isfinite(hit.score):
            raise HybridFusionError("FUSION_INPUT_INVALID", "hit score must be finite")
        if not isinstance(hit.payload, Mapping):
            raise HybridFusionError("FUSION_INPUT_INVALID", "hit payload must be a mapping")

    def _fuse_authorized(
        self,
        dense_hits: Sequence[HybridHit],
        sparse_hits: Sequence[HybridHit],
        top_k: int,
    ) -> list[FusionResult]:
        dense = self._rank_map(dense_hits, "dense", self.policy.dense_weight)
        sparse = self._rank_map(sparse_hits, "sparse", self.policy.sparse_weight)
        candidate_ids = sorted(set(dense) | set(sparse))
        results: list[FusionResult] = []
        for candidate_id in candidate_ids:
            dense_item = dense.get(candidate_id)
            sparse_item = sparse.get(candidate_id)
            dense_rank = dense_item[0] if dense_item else None
            sparse_rank = sparse_item[0] if sparse_item else None
            score = 0.0
            if dense_rank is not None:
                score += self.policy.dense_weight / (self.policy.rrf_k + dense_rank)
            if sparse_rank is not None:
                score += self.policy.sparse_weight / (self.policy.rrf_k + sparse_rank)

            primary = dense_item[1] if dense_item else sparse_item[1]
            payload = dict(primary.payload)
            if dense_item and sparse_item:
                for key, value in sparse_item[1].payload.items():
                    payload.setdefault(key, value)
            channels = tuple(
                channel
                for channel, item in (("dense", dense_item), ("sparse", sparse_item))
                if item is not None
            )
            results.append(
                FusionResult(
                    candidate_id=candidate_id,
                    tenant_id=primary.tenant_id,
                    score=score,
                    dense_rank=dense_rank,
                    sparse_rank=sparse_rank,
                    source_channels=channels,
                    payload=payload,
                )
            )
        results.sort(key=lambda result: (-result.score, result.candidate_id))
        return results[:top_k]

    @staticmethod
    def _rank_map(
        hits: Sequence[HybridHit],
        channel: str,
        weight: float,
    ) -> dict[str, tuple[int, HybridHit]]:
        if weight == 0:
            return {}
        ranked: dict[str, tuple[int, HybridHit]] = {}
        for rank, hit in enumerate(hits, start=1):
            # The first occurrence is authoritative for a malformed duplicate;
            # it also makes retries from an upstream adapter deterministic.
            ranked.setdefault(hit.candidate_id, (rank, hit))
        return ranked


__all__ = [
    "FusionResult",
    "HybridAuthorizationError",
    "HybridFusionEngine",
    "HybridFusionError",
    "HybridFusionPolicy",
    "HybridHit",
]
