"""Unit tests for EAM quality:confirm (no Postgres required)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from enterprise.gateway.quality.confirm import (
    ConfirmQualityError,
    confirm_document_quality,
)


def _doc(**overrides):
    base = dict(
        id=10,
        tenant_id="customer-a",
        source_system="DEMO",
        external_document_id="DOC-1",
        source_version_id="v1",
        sync_status="ready",
        business_status="active",
        current_version=0,
        ragflow_dataset_id="ds-1",
        ragflow_document_id="rf-1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _eval(**overrides):
    base = dict(
        id=1,
        parse_quality_status="review_required",
        quality_reasons=["EMPTY_PAGE_RATIO"],
        metrics_json={"chunk_count": 1},
        parse_repeatability_hash=None,
        e2e_repeatability_hash=None,
        artifact_hash=None,
        enterprise_commit=None,
        enterprise_worktree_dirty=False,
        ragflow_source_tag=None,
        ragflow_source_commit=None,
        thresholds_version="t1",
        thresholds_digest="d1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_confirm_requires_actor_reason():
    with pytest.raises(ConfirmQualityError) as ei:
        await confirm_document_quality(
            db=object(),
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-1",
            source_version_id="v1",
            actor="",
            reason="ok",
            decision_id="d1",
            ragflow_client=object(),
        )
    assert ei.value.code == "VALIDATION_ERROR"
    assert ei.value.http_status == 422


@pytest.mark.asyncio
async def test_confirm_disabled_is_review_already_closed():
    doc = _doc(business_status="disabled")
    with patch("enterprise.gateway.quality.confirm.gw_read", new=AsyncMock(return_value=doc)):
        with pytest.raises(ConfirmQualityError) as ei:
            await confirm_document_quality(
                db=object(),
                tenant_id="customer-a",
                source_system="DEMO",
                external_document_id="DOC-1",
                source_version_id="v1",
                actor="eam",
                reason="release",
                decision_id="d1",
                ragflow_client=object(),
            )
    assert ei.value.code == "REVIEW_ALREADY_CLOSED"
    assert ei.value.http_status == 409


@pytest.mark.asyncio
async def test_confirm_idempotent_when_passed_and_current():
    doc = _doc(current_version=1, business_status="active", sync_status="ready")
    evaluation = _eval(parse_quality_status="passed")

    async def fake_read(db, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_mapping":
            return doc
        if name == "get_latest_evaluation":
            return evaluation
        raise AssertionError(name)

    with patch("enterprise.gateway.quality.confirm.gw_read", new=fake_read):
        result = await confirm_document_quality(
            db=object(),
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-1",
            source_version_id="v1",
            actor="eam",
            reason="replay",
            decision_id="d1",
            ragflow_client=object(),
        )
    assert result.idempotent is True
    assert result.promoted is True
    assert result.parse_quality_status == "passed"


@pytest.mark.asyncio
async def test_confirm_non_latest_conflict():
    old = _doc(id=1, source_version_id="v1")
    new = _doc(id=2, source_version_id="v2")
    evaluation = _eval()

    async def fake_read(db, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_mapping":
            return old
        if name == "get_latest_evaluation":
            return evaluation
        if name == "get_versions_for_document":
            return [old, new]
        raise AssertionError(name)

    with patch("enterprise.gateway.quality.confirm.gw_read", new=fake_read):
        with pytest.raises(ConfirmQualityError) as ei:
            await confirm_document_quality(
                db=object(),
                tenant_id="customer-a",
                source_system="DEMO",
                external_document_id="DOC-1",
                source_version_id="v1",
                actor="eam",
                reason="release",
                decision_id="d1",
                ragflow_client=object(),
            )
    assert ei.value.code == "CONFLICT"


@pytest.mark.asyncio
async def test_confirm_happy_path_writes_passed_and_promotes():
    doc = _doc(id=5, current_version=0)
    promoted_doc = _doc(id=5, current_version=1, business_status="active")
    evaluation = _eval()
    writes = []

    async def fake_read(db, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_mapping":
            # after promote, return promoted
            if writes:
                return promoted_doc
            return doc
        if name == "get_latest_evaluation":
            return evaluation
        if name == "get_versions_for_document":
            return [doc]
        raise AssertionError(name)

    async def fake_write(db, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        writes.append(name)
        if name == "complete_evaluation":
            assert kwargs["parse_quality_status"] == "passed"
            assert kwargs["metrics_json"]["manualConfirm"]["actor"] == "eam"
            return None
        if name == "promote_quality_passed_version":
            return True
        raise AssertionError(name)

    with patch("enterprise.gateway.quality.confirm.gw_read", new=fake_read), patch(
        "enterprise.gateway.quality.confirm.gw_write", new=fake_write
    ):
        result = await confirm_document_quality(
            db=object(),
            tenant_id="customer-a",
            source_system="DEMO",
            external_document_id="DOC-1",
            source_version_id="v1",
            actor="eam",
            reason="ok to release",
            decision_id="d1",
            ragflow_client=object(),
        )
    assert result.idempotent is False
    assert result.promoted is True
    assert result.current_version == 1
    assert "complete_evaluation" in writes
    assert "promote_quality_passed_version" in writes
