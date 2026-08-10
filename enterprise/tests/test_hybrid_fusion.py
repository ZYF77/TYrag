"""Unit and contract tests for the independent hybrid fusion core."""

from __future__ import annotations

import math

import pytest

from enterprise.gateway.acl.schema import DocumentAclFacts
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query.hybrid_fusion import (
    HybridAuthorizationError,
    HybridFusionEngine,
    HybridFusionError,
    HybridFusionPolicy,
    HybridHit,
)


def _principal(
    *,
    tenant_id: str = "tenant-a",
    security_level: int = 3,
    groups: tuple[str, ...] = ("maintenance",),
    mapping_status: str = "active",
) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant_id,
        business_user_id="user-a",
        subject="subject-a",
        department_ids=("dept-maintenance",),
        group_ids=groups,
        security_level=security_level,
        mapping_status=mapping_status,
        capabilities=("ask", "view_citations"),
    )


def _facts(
    *,
    tenant_id: str = "tenant-a",
    department_id: str | None = "dept-maintenance",
    security_level: int | None = 2,
    allow_groups: tuple[str, ...] = ("maintenance",),
    deny_groups: tuple[str, ...] = (),
    status: str = "active",
) -> DocumentAclFacts:
    return DocumentAclFacts(
        tenant_id=tenant_id,
        department_id=department_id,
        security_level=security_level,
        allow_group_ids=allow_groups,
        deny_group_ids=deny_groups,
        business_status=status,
    )


def _hit(
    candidate_id: str,
    score: float,
    *,
    tenant_id: str = "tenant-a",
    facts: DocumentAclFacts | None = None,
    payload: dict | None = None,
) -> HybridHit:
    return HybridHit(
        candidate_id=candidate_id,
        tenant_id=tenant_id,
        score=score,
        payload=payload or {"title": candidate_id},
        acl_facts=facts if facts is not None else _facts(tenant_id=tenant_id),
    )


def test_fusion_contract_exports_stable_channels_and_result_shape():
    result = HybridFusionEngine().fuse(
        _principal(),
        [_hit("doc-1", 0.9)],
        [_hit("doc-1", 0.8)],
    )[0]

    assert result.candidate_id == "doc-1"
    assert result.tenant_id == "tenant-a"
    assert result.dense_rank == 1
    assert result.sparse_rank == 1
    assert result.source_channels == ("dense", "sparse")
    assert result.payload["title"] == "doc-1"


def test_weighted_rrf_combines_channels_and_has_deterministic_tie_break():
    engine = HybridFusionEngine(
        HybridFusionPolicy(dense_weight=0.7, sparse_weight=0.3, rrf_k=1, top_k=10)
    )
    results = engine.fuse(
        _principal(),
        [_hit("both", 1.0), _hit("dense-only", 0.8)],
        [_hit("both", 0.2), _hit("sparse-only", 0.9)],
    )

    assert [item.candidate_id for item in results] == [
        "both",
        "dense-only",
        "sparse-only",
    ]
    assert results[0].score == pytest.approx(0.5)
    assert results[0].source_channels == ("dense", "sparse")

    tied = HybridFusionEngine(HybridFusionPolicy(rrf_k=1)).fuse(
        _principal(),
        [_hit("b", 1.0), _hit("a", 1.0)],
        [_hit("a", 1.0), _hit("b", 1.0)],
    )
    assert [item.candidate_id for item in tied] == ["a", "b"]


def test_duplicate_channel_keeps_first_rank_and_merges_payload_without_overwrite():
    results = HybridFusionEngine().fuse(
        _principal(),
        [
            _hit("doc-1", 0.9, payload={"title": "dense", "page": 3}),
            _hit("doc-1", 0.1, payload={"title": "duplicate", "page": 9}),
        ],
        [_hit("doc-1", 0.8, payload={"title": "sparse", "excerpt": "safe"})],
    )

    assert len(results) == 1
    assert results[0].dense_rank == 1
    assert results[0].payload == {
        "title": "dense",
        "page": 3,
        "excerpt": "safe",
    }


def test_acl_and_tenant_filter_happens_before_fusion():
    denied = _hit(
        "denied",
        1000.0,
        facts=_facts(deny_groups=("maintenance",)),
    )
    other_tenant = _hit("other-tenant", 1000.0, tenant_id="tenant-b")
    missing_facts = HybridHit(
        candidate_id="missing-acl",
        tenant_id="tenant-a",
        score=1000.0,
        payload={"title": "must not leak"},
        acl_facts=None,
    )
    results = HybridFusionEngine().fuse(
        _principal(),
        [denied, other_tenant, missing_facts],
        [_hit("allowed", 0.1)],
    )

    assert [item.candidate_id for item in results] == ["allowed"]


def test_inactive_or_missing_principal_fails_closed():
    engine = HybridFusionEngine()
    with pytest.raises(HybridAuthorizationError) as inactive:
        engine.fuse(_principal(mapping_status="disabled"), [], [])
    assert inactive.value.code == "FUSION_AUTHORIZATION_REQUIRED"

    with pytest.raises(HybridAuthorizationError):
        engine.fuse(None, [], [])


@pytest.mark.parametrize(
    "policy",
    [
        HybridFusionPolicy(dense_weight=1.0, sparse_weight=0.0),
        HybridFusionPolicy(dense_weight=0.0, sparse_weight=1.0),
    ],
)
def test_zero_weight_channel_is_not_allowed_to_influence_result(policy):
    results = HybridFusionEngine(policy).fuse(
        _principal(),
        [_hit("dense", 1.0)],
        [_hit("sparse", 1.0)],
    )
    expected = "dense" if policy.dense_weight else "sparse"
    assert [item.candidate_id for item in results] == [expected]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dense_weight": -0.1},
        {"dense_weight": math.nan},
        {"dense_weight": 0.0, "sparse_weight": 0.0},
        {"rrf_k": 0},
        {"top_k": 0},
        {"top_k": 101},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(HybridFusionError) as error:
        HybridFusionPolicy(**kwargs)
    assert error.value.code == "FUSION_POLICY_INVALID"


def test_invalid_hit_is_rejected_without_logging_content():
    invalid = HybridHit(
        candidate_id="valid-id",
        tenant_id="tenant-a",
        score=math.inf,
        payload={"excerpt": "sensitive content must not be logged"},
        acl_facts=_facts(),
    )
    with pytest.raises(HybridFusionError) as error:
        HybridFusionEngine().fuse(_principal(), [invalid], [])
    assert error.value.code == "FUSION_INPUT_INVALID"
    assert "sensitive content" not in str(error.value)
