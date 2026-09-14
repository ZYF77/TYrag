import asyncio
from types import SimpleNamespace

import pytest
import rag.advanced_rag.harness.tools  # noqa: F401  populate TOOL_REGISTRY
from rag.advanced_rag.agentic_rag import RAGTools
from rag.advanced_rag.harness.config import THINKING_MODES
from rag.advanced_rag.harness.agent import _fmt_tool_result, execute_with_fallback
from rag.advanced_rag.harness.planner import planner_node
from rag.advanced_rag.harness.tools.gating import get_gated_tools
from rag.advanced_rag.harness.tools.navigation import (
    _restricted_empty_scope as navigation_restricted_empty_scope,
)
from rag.advanced_rag.harness.tools.search import (
    _restricted_empty_scope as search_restricted_empty_scope,
)
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


def test_high_profile_does_not_allow_standalone_bm25_or_wiki():
    available = set(THINKING_MODES["high"].available_tools)
    assert "bm25_search" not in available
    assert "wiki_query" not in available


@pytest.mark.asyncio
async def test_high_empty_hybrid_does_not_escape_to_bm25_or_wiki():
    class Pipeline:
        tools = SimpleNamespace(doc_scope_mode=None, doc_scope=["doc-1"])

        def __init__(self):
            self.calls = []

        async def execute(self, name, **kwargs):
            self.calls.append((name, kwargs))
            from rag.advanced_rag.harness.types import ToolResult

            return ToolResult()

    pipeline = Pipeline()
    result = await execute_with_fallback(
        pipeline,
        "hybrid_search",
        "explore",
        allowed_tools={"hybrid_search"},
        query="equipment",
    )

    assert result.chunks == []
    assert [name for name, _ in pipeline.calls] == ["hybrid_search"]


@pytest.mark.asyncio
async def test_allowed_hybrid_fallback_uses_bm25_once_with_shared_arguments():
    class Pipeline:
        tools = SimpleNamespace(doc_scope_mode=None, doc_scope=["doc-1"])

        def __init__(self):
            self.calls = []

        async def execute(self, name, **kwargs):
            self.calls.append((name, kwargs))
            from rag.advanced_rag.harness.types import ToolResult

            return ToolResult(
                chunks=[{"chunk_id": "bm25"}] if name == "bm25_search" else []
            )

    pipeline = Pipeline()
    result = await execute_with_fallback(
        pipeline,
        "hybrid_search",
        "explore",
        allowed_tools={"hybrid_search", "bm25_search"},
        query="equipment",
        keywords="model",
    )

    assert [name for name, _ in pipeline.calls] == ["hybrid_search", "bm25_search"]
    assert pipeline.calls[1][1] == {"query": "equipment", "keywords": "model"}
    assert result.metadata["fallback_from"] == "hybrid_search"


@pytest.mark.asyncio
async def test_navigation_docs_are_payload_and_topic_is_not_sent_to_hybrid():
    class Pipeline:
        tools = SimpleNamespace(doc_scope_mode=None, doc_scope=["doc-1"])

        def __init__(self):
            self.calls = []

        async def execute(self, name, **kwargs):
            self.calls.append((name, kwargs))
            from rag.advanced_rag.harness.types import ToolResult

            return ToolResult(docs=["doc-1"])

    pipeline = Pipeline()
    result = await execute_with_fallback(
        pipeline,
        "dataset_navigation_by_tree",
        "locate",
        allowed_tools={"dataset_navigation_by_tree", "hybrid_search"},
        topic="equipment",
    )

    assert [name for name, _ in pipeline.calls] == ["dataset_navigation_by_tree"]
    assert "located documents: doc-1" in _fmt_tool_result(result)


def test_tool_argument_validation_rejects_cross_tool_parameter_names():
    class Pipeline:
        tools = SimpleNamespace(doc_scope_mode=None, doc_scope=["doc-1"])

    import asyncio

    result = asyncio.run(
        execute_with_fallback(
            Pipeline(),
            "hybrid_search",
            "locate",
            allowed_tools={"hybrid_search"},
            query="equipment",
            topic="equipment",
        )
    )

    assert result.error and "unsupported arguments" in result.error


def test_scoped_doc_ids_intersects_hard_filter():
    tools = SimpleNamespace(doc_scope=["doc-keep"])
    assert RAGTools.scoped_doc_ids(tools, ["doc-keep", "doc-drop"]) == ["doc-keep"]
    assert RAGTools.scoped_doc_ids(tools, []) == []
    assert RAGTools.scoped_doc_ids(tools, None) == ["doc-keep"]
    open_tools = SimpleNamespace(doc_scope=None)
    assert RAGTools.scoped_doc_ids(open_tools, ["doc-a"]) == ["doc-a"]


def test_harness_retrieval_tools_distinguish_restricted_empty_scope():
    restricted = SimpleNamespace(doc_scope=[], doc_scope_mode="restrict")
    open_tools = SimpleNamespace(doc_scope=None, doc_scope_mode=None)

    assert search_restricted_empty_scope(restricted, []) is True
    assert navigation_restricted_empty_scope(restricted, []) is True
    assert search_restricted_empty_scope(open_tools, []) is False
    assert navigation_restricted_empty_scope(open_tools, []) is False


@pytest.mark.asyncio
async def test_agentic_metadata_filter_receives_rewritten_question(monkeypatch):
    class Chat:
        max_length = 4096

        async def async_chat(self, *_args, **_kwargs):
            return '{"question": "rewritten question", "keywords": "rewritten"}'

    tools = RAGTools.__new__(RAGTools)
    tools.chat_mdl = Chat()
    tools.business_context = {}
    tools.meta_data_filter = {
        "method": "manual",
        "manual": [{"key": "type", "op": "=", "value": "pump"}],
    }
    tools.doc_scope = ["doc-g"]
    tools.doc_scope_mode = "restrict"
    tools.metadata_kb_ids = ["kb-1"]
    calls = []

    async def _apply(_filter, _metas, question, _chat, scope, **_kwargs):
        calls.append((question, scope))
        return ["doc-g"]

    monkeypatch.setattr(
        "rag.advanced_rag.agentic_rag.message_fit_in",
        lambda messages, _max_length: (0, messages),
    )
    monkeypatch.setattr(
        "rag.advanced_rag.agentic_rag.apply_meta_data_filter", _apply
    )

    question, _keywords = await tools.formalize(
        [{"role": "user", "content": "raw follow-up"}]
    )

    assert question == "rewritten question"
    assert calls == [("rewritten question", ["doc-g"])]


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
@pytest.mark.asyncio
async def test_rag_stream_callback_emits_visible_chunks_and_suppresses_tool_result(monkeypatch):
    from rag.advanced_rag import agentic_rag_graph

    async def fake_run(_tools, _messages):
        yield "<think>planning</think>"
        yield "first answer chunk"
        yield "second answer chunk"

    monkeypatch.setattr(agentic_rag_graph, "run_agentic_rag", fake_run)
    tools = RAGTools.__new__(RAGTools)
    emitted = []

    async def callback(value):
        emitted.append(value)

    tools.stream_callback = callback

    result = await tools.rag("question")

    assert emitted == ["first answer chunk", "second answer chunk"]
    assert result == ""


@pytest.mark.asyncio
async def test_agentic_graph_failure_raises_instead_of_yielding_success_text(monkeypatch):
    from rag.advanced_rag import agentic_rag_graph

    class FailingGraph:
        async def ainvoke(self, *_args, **_kwargs):
            raise ValueError("controlled graph failure")

    monkeypatch.setattr(
        agentic_rag_graph,
        "build_agentic_graph",
        lambda *_args, **_kwargs: FailingGraph(),
    )

    with pytest.raises(RuntimeError, match="Agentic RAG graph execution failed"):
        async for _ in agentic_rag_graph.run_agentic_rag(
            SimpleNamespace(), [{"role": "user", "content": "question"}]
        ):
            pass
@pytest.mark.asyncio
async def test_agentic_graph_cancellation_cancels_running_graph(monkeypatch):
    from rag.advanced_rag import agentic_rag_graph

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowGraph:
        async def ainvoke(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(
        agentic_rag_graph,
        "build_agentic_graph",
        lambda *_args, **_kwargs: SlowGraph(),
    )

    stream = agentic_rag_graph.run_agentic_rag(
        SimpleNamespace(), [{"role": "user", "content": "question"}]
    )
    next_item = asyncio.create_task(stream.__anext__())
    await started.wait()
    next_item.cancel()

    with pytest.raises(asyncio.CancelledError):
        await next_item
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await stream.aclose()
