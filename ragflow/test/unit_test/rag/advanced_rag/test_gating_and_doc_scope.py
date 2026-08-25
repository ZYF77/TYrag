from types import SimpleNamespace

import pytest
import rag.advanced_rag.harness.tools  # noqa: F401  populate TOOL_REGISTRY
from rag.advanced_rag.agentic_rag import RAGTools
from rag.advanced_rag.harness.config import THINKING_MODES
from rag.advanced_rag.harness.planner import planner_node
from rag.advanced_rag.harness.tools.gating import get_gated_tools
from rag.advanced_rag.harness.types import OrchestratorContext, RouteDecision


def _tool_names(defs):
    names = []
    for item in defs:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(function["name"])
        elif isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
    return names


def test_high_explore_omits_web_search_when_disabled():
    ctx = OrchestratorContext(question="q", claims=[], mode="high")
    defs = get_gated_tools(
        phase="explore",
        available_tools=list(THINKING_MODES["high"].available_tools),
        compilation_map={},
        context=ctx,
        web_enabled=False,
    )
    assert "web_search" not in _tool_names(defs)


def test_high_explore_keeps_web_search_when_enabled():
    ctx = OrchestratorContext(question="q", claims=[], mode="high")
    defs = get_gated_tools(
        phase="explore",
        available_tools=list(THINKING_MODES["high"].available_tools),
        compilation_map={},
        context=ctx,
        web_enabled=True,
    )
    assert "web_search" in _tool_names(defs)


def test_scoped_doc_ids_intersects_hard_filter():
    tools = SimpleNamespace(doc_scope=["doc-keep"])
    assert RAGTools.scoped_doc_ids(tools, ["doc-keep", "doc-drop"]) == ["doc-keep"]
    assert RAGTools.scoped_doc_ids(tools, None) == ["doc-keep"]
    open_tools = SimpleNamespace(doc_scope=None)
    assert RAGTools.scoped_doc_ids(open_tools, ["doc-a"]) == ["doc-a"]


def test_scope_filter_drops_outside_missing_and_ambiguous_compiled_chunks():
    tools = object.__new__(RAGTools)
    tools.doc_scope = ["doc-keep"]
    tools.web_search = None
    scoped = tools.enforce_doc_scope(
        {
            "chunks": [
                {"chunk_id": "allowed", "doc_id": "doc-keep"},
                {"chunk_id": "outside", "doc_id": "doc-drop"},
                {"chunk_id": "missing"},
                {
                    "chunk_id": "wiki-single",
                    "doc_id": "wiki-page",
                    "source_doc_ids": ["doc-keep"],
                },
                {
                    "chunk_id": "wiki-mixed",
                    "doc_id": "wiki-mixed",
                    "source_doc_ids": ["doc-keep", "doc-drop"],
                },
            ],
            "doc_aggs": [
                {"doc_id": "doc-keep"},
                {"doc_id": "doc-drop"},
            ],
        }
    )
    assert [chunk["chunk_id"] for chunk in scoped["chunks"]] == [
        "allowed",
        "wiki-single",
    ]
    assert scoped["chunks"][1]["doc_id"] == "doc-keep"
    assert {agg["doc_id"] for agg in scoped["doc_aggs"]} == {"doc-keep"}


def test_scope_filter_keeps_valid_web_only_when_web_is_enabled():
    chunk = {"chunk_id": "web", "doc_id": "outside", "url": "https://example.com/a"}
    tools = object.__new__(RAGTools)
    tools.doc_scope = ["doc-keep"]
    tools.web_search = None
    assert tools.enforce_doc_scope({"chunks": [chunk], "doc_aggs": []})["chunks"] == []
    tools.web_search = object()
    assert tools.enforce_doc_scope({"chunks": [chunk], "doc_aggs": []})["chunks"] == [chunk]


@pytest.mark.asyncio
async def test_planner_falls_back_when_model_returns_json_array():
    class Chat:
        max_length = 4096

        async def async_chat(self, *args, **kwargs):
            return '[{"claims": []}]'

    class Tools:
        chat_mdl = Chat()

        async def _fit_messages(self, system, user):
            return [{"content": system}, {"content": user}]

    route = RouteDecision(
        question="compare",
        thinking_mode="ultra",
        question_type="comparative",
        requires_decomposition=True,
        suggests_compilation=None,
        execution_strategy="deep_research",
    )
    result = await planner_node({"route": route, "seed_chunks": []}, Tools())
    assert result["plan"].plan_type == "direct"
