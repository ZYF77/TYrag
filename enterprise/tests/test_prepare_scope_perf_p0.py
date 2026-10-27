"""P0 Gateway prepare: batch evaluations, no second full scope, early SSE open."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from enterprise.gateway.acl.schema import AclScope
from enterprise.gateway.quality.models import QualityEvaluation
from enterprise.gateway.sync import readiness as readiness_mod
from enterprise.gateway.sync.readiness import (
    document_candidate_readiness_from_evaluation,
    document_evaluation_key,
    evaluation_lookup_key,
    get_latest_evaluations_batch,
)


def _eval(**overrides) -> QualityEvaluation:
    base = dict(
        id=1,
        tenant_id="t1",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="v1",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-1",
        evaluation_version="1",
        evaluation_contract_version="1",
        thresholds_version=None,
        thresholds_digest=None,
        parser_profile=None,
        parser_version=None,
        routing_policy_version=None,
        evaluation_state="completed",
        parse_quality_status="passed",
    )
    base.update(overrides)
    return QualityEvaluation(**base)


def _doc(**overrides):
    base = dict(
        tenant_id="t1",
        source_system="EAM",
        external_document_id="DOC-1",
        source_version_id="v1",
        source_kind="S3",
        current_version=1,
        business_status="active",
        sync_status="ready",
        pipeline_status="DONE",
        event_status="completed",
        source_state="AVAILABLE",
        ragflow_dataset_id="ds-1",
        ragflow_document_id="doc-1",
        equipment_id=None,
        fixed_asset_no=None,
        asset_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_readiness_from_evaluation_matches_pass_and_fail(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")

    # No evaluation + quality not required => retrievable (legacy S3 path).
    doc = _doc()
    ok, status = document_candidate_readiness_from_evaluation(doc, None)
    assert ok.retrievable is True
    assert status is None

    # Passed evaluation with gate stubbed open.
    monkeypatch.setattr(
        "enterprise.gateway.sync.readiness.enforce_quality_gate",
        lambda evaluation, **kwargs: (True, None),
    )
    ok2, status2 = document_candidate_readiness_from_evaluation(doc, _eval())
    assert ok2.retrievable is True
    assert status2 == "passed"

    pending_doc = _doc(source_kind="FILE_SHARE")
    blocked, reason_status = document_candidate_readiness_from_evaluation(
        pending_doc, None
    )
    assert blocked.retrievable is False
    assert blocked.blocking_reason == "DOCUMENT_QUALITY_PENDING"
    assert reason_status is None


@pytest.mark.asyncio
async def test_batch_evaluations_uses_single_fetchall(monkeypatch):
    calls = {"n": 0}
    rows = [
        {
            "id": 2,
            "tenant_id": "t1",
            "source_system": "EAM",
            "external_document_id": "DOC-1",
            "source_version_id": "v1",
            "ragflow_dataset_id": "ds-1",
            "ragflow_document_id": "doc-1",
            "evaluation_version": "1",
            "evaluation_contract_version": "1",
            "thresholds_version": None,
            "thresholds_digest": None,
            "parser_profile": None,
            "parser_version": None,
            "routing_policy_version": None,
            "routing_reasons": "[]",
            "evaluation_state": "completed",
            "parse_quality_status": "pass",
            "quality_reasons": "[]",
            "metrics_json": "{}",
            "parse_repeatability_hash": None,
            "e2e_repeatability_hash": None,
            "artifact_hash": None,
            "enterprise_commit": None,
            "enterprise_worktree_dirty": 0,
            "ragflow_source_tag": None,
            "ragflow_source_commit": None,
            "attempt_count": 1,
            "last_error_code": None,
            "last_error_message": None,
            "started_at": None,
            "completed_at": None,
            "created_at": "now",
            "updated_at": "now",
        },
        {
            "id": 1,
            "tenant_id": "t1",
            "source_system": "EAM",
            "external_document_id": "DOC-2",
            "source_version_id": "v1",
            "ragflow_dataset_id": "ds-1",
            "ragflow_document_id": "doc-2",
            "evaluation_version": "1",
            "evaluation_contract_version": "1",
            "thresholds_version": None,
            "thresholds_digest": None,
            "parser_profile": None,
            "parser_version": None,
            "routing_policy_version": None,
            "routing_reasons": "[]",
            "evaluation_state": "completed",
            "parse_quality_status": "pass",
            "quality_reasons": "[]",
            "metrics_json": "{}",
            "parse_repeatability_hash": None,
            "e2e_repeatability_hash": None,
            "artifact_hash": None,
            "enterprise_commit": None,
            "enterprise_worktree_dirty": 0,
            "ragflow_source_tag": None,
            "ragflow_source_commit": None,
            "attempt_count": 1,
            "last_error_code": None,
            "last_error_message": None,
            "started_at": None,
            "completed_at": None,
            "created_at": "now",
            "updated_at": "now",
        },
    ]

    async def fake_fetchall(conn, sql, params=None):
        calls["n"] += 1
        assert "DISTINCT ON" in sql
        assert params[0] == "t1"
        return rows

    monkeypatch.setattr(readiness_mod, "fetchall", fake_fetchall)
    out = await get_latest_evaluations_batch(
        object(),
        tenant_id="t1",
        source_system="EAM",
        docs=[_doc(external_document_id="DOC-1"), _doc(external_document_id="DOC-2", ragflow_document_id="doc-2")],
    )
    assert calls["n"] == 1
    assert evaluation_lookup_key("t1", "EAM", "DOC-1", "v1") in out
    assert evaluation_lookup_key("t1", "EAM", "DOC-2", "v1") in out


@pytest.mark.asyncio
async def test_context_scope_reuses_turn_snapshot_without_available_scan(monkeypatch):
    from enterprise.gateway.query import v2_router as v2

    calls = {"available": 0}

    async def boom(*args, **kwargs):
        calls["available"] += 1
        raise AssertionError("_available_context_scope must not run when turn snapshot exists")

    monkeypatch.setattr(v2, "_available_context_scope", boom)
    doc = _doc(ragflow_document_id="doc-keep")
    conversation = {
        "_turn_scope_resolved": True,
        "_turn_document_ids": ["doc-keep"],
        "_turn_docs_by_id": {"doc-keep": doc},
        "_turn_policy_version": "pv-1",
        "equipment_id": None,
        "fixed_asset_no": None,
    }
    scope, docs = await v2._context_scope(object(), object(), conversation)
    assert calls["available"] == 0
    assert list(scope.document_ids) == ["doc-keep"]
    assert docs == {"doc-keep": doc}


@pytest.mark.asyncio
async def test_prepare_defer_scope_skips_resolve_until_ensure(monkeypatch):
    from enterprise.gateway.query import v2_router as v2

    resolve_calls = {"n": 0}

    async def fake_resolve(db, principal, conversation, question):
        resolve_calls["n"] += 1
        conversation = dict(conversation)
        conversation["_turn_scope_resolved"] = True
        conversation["_turn_entity_ids"] = []
        conversation["_turn_document_ids"] = ["doc-a"]
        conversation["_turn_docs_by_id"] = {"doc-a": _doc(ragflow_document_id="doc-a")}
        conversation["_turn_policy_version"] = "pv"
        return conversation

    async def fake_owned(db, principal, conversation_id):
        return {
            "conversation_id": conversation_id,
            "tenant_id": "t1",
            "business_user_id": "u1",
            "context_version": 1,
            "equipment_id": None,
            "fixed_asset_no": None,
            "status": "active",
        }

    async def fake_gw_write(db, fn, /, *args, **kwargs):
        if getattr(fn, "__name__", "") == "expire_conversation_run":
            return None
        if getattr(fn, "__name__", "") == "reserve_message_run":
            return {
                "run_id": "run-1",
                "assistant_message_id": "asst-1",
                "status": "running",
            }
        if getattr(fn, "__name__", "") == "save_run_scope":
            return None
        return None

    async def fake_replay(*args, **kwargs):
        return None, None

    monkeypatch.setattr(v2, "_resolve_turn_scope", fake_resolve)
    monkeypatch.setattr(v2, "_owned_conversation", fake_owned)
    monkeypatch.setattr(v2, "_gw_write", fake_gw_write)
    monkeypatch.setattr(v2, "_replay_or_pending", fake_replay)
    monkeypatch.setattr(v2, "_validate_reasoning_mode_access", lambda *a, **k: None)
    monkeypatch.setattr(v2, "_request_hash", lambda *a, **k: "hash")
    monkeypatch.setattr(v2, "_suggestions", lambda *a, **k: [])

    class Req:
        suggestionId = None
        question = "hello"
        clientMessageId = "c1"
        reasoningMode = "simple"
        contextVersion = 1

    conversation = await fake_owned(None, None, "conv-1")
    principal = SimpleNamespace(tenant_id="t1", business_user_id="u1")
    conv2, question, run, err = await v2._prepare_message_run(
        object(), principal, conversation, Req(), [], defer_scope=True
    )
    assert err is None
    assert run["_scope_deferred"] is True
    assert resolve_calls["n"] == 0

    ensured = await v2._ensure_turn_scope_for_run(
        object(), principal, conv2, Req(), question, run
    )
    assert resolve_calls["n"] == 1
    assert ensured["_turn_scope_resolved"] is True
    assert run.get("_scope_deferred") is None


def test_available_context_scope_source_has_no_per_doc_gw_read():
    from pathlib import Path

    src = Path("enterprise/gateway/query/v2_router.py").read_text(encoding="utf-8")
    # Locate _available_context_scope body roughly
    start = src.index("async def _available_context_scope")
    end = src.index("async def _context_scope", start)
    body = src[start:end]
    assert "document_candidate_readiness_from_evaluation" in body
    assert "await _gw_read" not in body
    assert "document_candidate_readiness_from_db" not in body


def test_benchmark_cpu_readiness_for_3k_docs(monkeypatch):
    """CPU-only gate over ~3k docs should be well under a second (no DB N+1)."""
    monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
    monkeypatch.setenv("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false")
    monkeypatch.setattr(
        "enterprise.gateway.sync.readiness.enforce_quality_gate",
        lambda evaluation, **kwargs: (True, None),
    )
    docs = [
        _doc(
            external_document_id=f"DOC-{i}",
            ragflow_document_id=f"doc-{i}",
            source_version_id="v1",
        )
        for i in range(3000)
    ]
    evaluations = {
        document_evaluation_key(doc): _eval(
            id=i + 1,
            external_document_id=doc.external_document_id,
            ragflow_document_id=doc.ragflow_document_id,
        )
        for i, doc in enumerate(docs)
    }
    t0 = time.perf_counter()
    allowed = []
    for doc in docs:
        readiness, _ = document_candidate_readiness_from_evaluation(
            doc, evaluations.get(document_evaluation_key(doc))
        )
        if readiness.retrievable:
            allowed.append(doc.ragflow_document_id)
    elapsed = time.perf_counter() - t0
    assert len(allowed) == 3000
    # Historical probe: Python N+1 await/txn was ~5s; CPU batch gate is ms-level.
    assert elapsed < 0.5, f"CPU readiness over 3k docs took {elapsed:.3f}s"
