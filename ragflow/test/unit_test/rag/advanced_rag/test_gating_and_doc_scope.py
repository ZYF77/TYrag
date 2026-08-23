from types import SimpleNamespace

import rag.advanced_rag.harness.tools  # noqa: F401  populate TOOL_REGISTRY
from rag.advanced_rag.agentic_rag import RAGTools
from rag.advanced_rag.harness.config import THINKING_MODES
from rag.advanced_rag.harness.tools.gating import get_gated_tools
from rag.advanced_rag.harness.types import OrchestratorContext


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
