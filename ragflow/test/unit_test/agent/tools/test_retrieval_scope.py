"""Unit checks for the Agent Workflow Retrieval document ceiling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent.tools.retrieval as retrieval_module
from agent.tools.retrieval import Retrieval


def test_scope_filter_drops_expanded_chunks_and_aggregates():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [
                {"doc_id": "doc-keep", "content": "allowed"},
                {"doc_id": "doc-drop", "content": "outside"},
                {"document_id": "doc-keep", "content": "also allowed"},
            ],
            "doc_aggs": [
                {"doc_id": "doc-keep"},
                {"doc_id": "doc-drop"},
            ],
        },
        ["doc-keep"],
    )

    assert [item["content"] for item in result["chunks"]] == [
        "allowed",
        "also allowed",
    ]
    assert result["doc_aggs"] == [{"doc_id": "doc-keep"}]

def test_scope_filter_accepts_doc_id_kwd():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [
                {"doc_id_kwd": "doc-keep", "content": "allowed"},
                {"doc_id_kwd": "doc-drop", "content": "outside"},
            ],
            "doc_aggs": [{"doc_id": "doc-keep"}, {"doc_id": "doc-drop"}],
        },
        ["doc-keep"],
    )
    assert [item["content"] for item in result["chunks"]] == ["allowed"]
    assert result["doc_aggs"] == [{"doc_id": "doc-keep"}]


def test_scope_filter_empty_allowed_drops_all():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [{"doc_id": "doc-a", "content": "x"}],
            "doc_aggs": [{"doc_id": "doc-a"}],
        },
        [],
    )
    assert result["chunks"] == []
    assert result["doc_aggs"] == []


def _make_retrieval_node(monkeypatch, *, metadata_filter=None, strict=True):
    outputs = {}
    references = []
    retrieval_calls = []

    param = SimpleNamespace(
        dataset_ids=["kb-1"],
        kb_ids=[],
        memory_ids=[],
        doc_scope_ids=["doc-a", "doc-b"] if strict else [],
        doc_scope_mode="restrict" if strict else None,
        meta_data_filter=metadata_filter or {},
        empty_response="EMPTY_SCOPE",
        rerank_id="",
        cross_languages=[],
        toc_enhance=False,
        use_kg=False,
        top_n=8,
        top_k=64,
        similarity_threshold=0.1,
        keywords_similarity_weight=0.7,
    )
    canvas = SimpleNamespace(
        _tenant_id="tenant-1",
        get_tenant_id=lambda: "tenant-1",
        get_variable_value=lambda _key: None,
        add_reference=lambda chunks, aggs: references.append((chunks, aggs)),
    )
    node = object.__new__(Retrieval)
    node._param = param
    node._canvas = canvas
    node.variable_ref_patt = r"\{([^{}]+)\}"
    node._resolved_doc_scope_ids = lambda: list(param.doc_scope_ids)
    node.get_input_elements_from_text = lambda _text: {}
    node.string_format = lambda text, _vars: text
    node.check_if_canceled = lambda _stage: False
    node.set_output = lambda key, value: outputs.__setitem__(key, value)

    kb = SimpleNamespace(id="kb-1", embd_id="embedding-1", tenant_id="tenant-1")
    monkeypatch.setattr(retrieval_module.KnowledgebaseService, "get_by_ids", lambda _ids: [kb])
    monkeypatch.setattr(retrieval_module, "resolve_model_config", lambda *_args: object())
    monkeypatch.setattr(retrieval_module, "LLMBundle", lambda *_args: object())
    monkeypatch.setattr(retrieval_module, "label_question", lambda *_args: {})
    monkeypatch.setattr(retrieval_module, "kb_prompt", lambda *_args: ["formatted evidence"])

    async def retrieval(*args, **kwargs):
        retrieval_calls.append(kwargs)
        return {
            "chunks": [
                {"doc_id": "doc-a", "content": "A", "similarity": 0.9},
                {"doc_id": "doc-b", "content": "B", "similarity": 0.8},
                {"doc_id": "doc-out", "content": "outside", "similarity": 0.7},
            ],
            "doc_aggs": [
                {"doc_id": "doc-a"},
                {"doc_id": "doc-b"},
                {"doc_id": "doc-out"},
            ],
        }

    monkeypatch.setattr(
        retrieval_module.settings,
        "retriever",
        SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, _tenants: chunks),
    )
    return node, outputs, references, retrieval_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["manual", "auto", "semi_auto"])
async def test_restrict_metadata_zero_match_fails_closed(monkeypatch, method):
    node, outputs, references, retrieval_calls = _make_retrieval_node(
        monkeypatch, metadata_filter={"method": method, "manual": [{}]}
    )
    monkeypatch.setattr(
        retrieval_module,
        "apply_meta_data_filter",
        AsyncMock(return_value=[]),
    )

    await node._retrieve_kb("question")

    assert outputs == {"formalized_content": "EMPTY_SCOPE", "json": []}
    assert references == []
    assert retrieval_calls == []


@pytest.mark.asyncio
async def test_restrict_metadata_hit_passes_intersection_to_retrieval(monkeypatch):
    node, outputs, references, retrieval_calls = _make_retrieval_node(
        monkeypatch,
        metadata_filter={"method": "manual", "manual": [{"key": "equipment_id"}]},
    )
    monkeypatch.setattr(
        retrieval_module,
        "apply_meta_data_filter",
        AsyncMock(return_value=["doc-a", "doc-out"]),
    )

    await node._retrieve_kb("question")

    assert len(retrieval_calls) == 1
    assert retrieval_calls[0]["doc_ids"] == ["doc-a"]
    assert [chunk["doc_id"] for chunk in outputs["json"]] == ["doc-a"]
    assert [chunk["doc_id"] for chunk in references[0][0]] == ["doc-a"]


@pytest.mark.asyncio
async def test_restrict_without_metadata_uses_gateway_scope(monkeypatch):
    node, outputs, _references, retrieval_calls = _make_retrieval_node(monkeypatch)

    await node._retrieve_kb("question")

    assert retrieval_calls[0]["doc_ids"] == ["doc-a", "doc-b"]
    assert [chunk["doc_id"] for chunk in outputs["json"]] == ["doc-a", "doc-b"]


@pytest.mark.asyncio
async def test_empty_retrieval_clears_json_output(monkeypatch):
    node, outputs, references, retrieval_calls = _make_retrieval_node(monkeypatch)

    async def empty_retrieval(*_args, **kwargs):
        retrieval_calls.append(kwargs)
        return {"chunks": [], "doc_aggs": []}

    monkeypatch.setattr(retrieval_module.settings.retriever, "retrieval", empty_retrieval)

    await node._retrieve_kb("question")

    assert outputs == {"formalized_content": "EMPTY_SCOPE", "json": []}
    assert references == []


@pytest.mark.asyncio
async def test_restrict_second_zero_match_clears_previous_output(monkeypatch):
    node, outputs, references, retrieval_calls = _make_retrieval_node(
        monkeypatch,
        metadata_filter={"method": "manual", "manual": [{"key": "equipment_id"}]},
    )
    apply_filter = AsyncMock(side_effect=[["doc-a"], []])
    monkeypatch.setattr(retrieval_module, "apply_meta_data_filter", apply_filter)

    await node._retrieve_kb("first")
    assert outputs["json"]
    assert len(references) == 1

    await node._retrieve_kb("second")
    assert outputs == {"formalized_content": "EMPTY_SCOPE", "json": []}
    assert len(references) == 1
    assert len(retrieval_calls) == 1


@pytest.mark.asyncio
async def test_legacy_mode_preserves_none_metadata_fallback(monkeypatch):
    node, _outputs, _references, retrieval_calls = _make_retrieval_node(
        monkeypatch,
        metadata_filter={"method": "auto"},
        strict=False,
    )
    monkeypatch.setattr(
        retrieval_module,
        "apply_meta_data_filter",
        AsyncMock(return_value=None),
    )

    await node._retrieve_kb("question")

    assert retrieval_calls[0]["doc_ids"] is None
