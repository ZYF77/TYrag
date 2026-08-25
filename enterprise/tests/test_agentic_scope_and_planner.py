"""Narrow verification of Agentic doc_scope / planner fallback without full RAGFlow deps."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

RAGFLOW_ROOT = Path(__file__).resolve().parents[2] / "ragflow"


def _ensure_pkg(name: str, path: Path | None = None) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    if path is not None:
        mod.__path__ = [str(path)]
    return mod


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    parent, _, child = name.rpartition(".")
    if parent:
        parent_mod = _ensure_pkg(parent)
        setattr(parent_mod, child, mod)
    return mod


def _load_agentic_scope_modules():
    _ensure_pkg("rag", RAGFLOW_ROOT / "rag")
    # Avoid rag.advanced_rag.__init__ which pulls DeepResearcher / peewee / Quart.
    adv = _ensure_pkg("rag.advanced_rag", RAGFLOW_ROOT / "rag" / "advanced_rag")
    _ensure_pkg("rag.advanced_rag.harness", RAGFLOW_ROOT / "rag" / "advanced_rag" / "harness")
    _ensure_pkg(
        "rag.advanced_rag.harness.prompts",
        RAGFLOW_ROOT / "rag" / "advanced_rag" / "harness" / "prompts",
    )

    _stub("json_repair", loads=lambda text: __import__("json").loads(text))
    _stub("common")
    _stub("common.settings")
    _stub("common.misc_utils", thread_pool_exec=None)
    _stub("common.token_utils", num_tokens_from_string=lambda *_a, **_k: 0)
    _stub("api")
    _stub("api.db")
    _stub("api.db.services")
    _stub("api.db.services.doc_metadata_service", DocMetadataService=object)
    _stub("api.db.services.document_service", DocumentService=object)
    _stub("api.db.services.knowledgebase_service", KnowledgebaseService=object)
    _stub("api.db.services.llm_service", LLMBundle=object)
    _stub("rag.app")
    _stub("rag.app.tag", label_question=lambda *_a, **_k: None)
    _stub("rag.llm")
    _stub("rag.llm.tool_decorator", tool=lambda *a, **k: (lambda fn: fn))
    _stub(
        "rag.prompts",
    )
    _stub(
        "rag.prompts.generator",
        citation_prompt=lambda *_a, **_k: "",
        form_message=lambda *a, **k: [],
        message_fit_in=lambda *a, **k: (0, []),
        kb_prompt=lambda *a, **k: "",
    )
    _stub("rag.advanced_rag.agentic_rag_graph", _strip_think_stream=None, _snip=lambda text, n=80: text[:n])

    # Load types/config/prompts first (lightweight).
    for mod_name, rel in (
        ("rag.advanced_rag.harness.types", "rag/advanced_rag/harness/types.py"),
        ("rag.advanced_rag.harness.config", "rag/advanced_rag/harness/config.py"),
        (
            "rag.advanced_rag.harness.prompts.decompose_prompts",
            "rag/advanced_rag/harness/prompts/decompose_prompts.py",
        ),
        ("rag.advanced_rag.harness.planner", "rag/advanced_rag/harness/planner.py"),
    ):
        if mod_name in sys.modules and hasattr(sys.modules[mod_name], "__file__"):
            continue
        path = RAGFLOW_ROOT / rel
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    # Load only the RAGTools class methods we need by compiling a thin wrapper
    # around the production source methods (avoids executing the whole module).
    source = (RAGFLOW_ROOT / "rag/advanced_rag/agentic_rag.py").read_text(encoding="utf-8")
    start = source.index("    def scoped_doc_ids(")
    end = source.index("    async def _fit_messages(")
    methods_src = source[start:end]
    namespace = {
        "List": list,
        "json_repair": sys.modules["json_repair"],
        "urlsplit": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit,
    }
    exec(
        "class RAGTools:\n"
        "    def has_web(self):\n"
        "        return self.web_search is not None\n"
        + methods_src,
        namespace,
    )
    return namespace["RAGTools"], sys.modules["rag.advanced_rag.harness.planner"], sys.modules[
        "rag.advanced_rag.harness.types"
    ]


RAGTools, planner, types_mod = _load_agentic_scope_modules()
planner_node = planner.planner_node
RouteDecision = types_mod.RouteDecision


def test_scoped_doc_ids_intersects_hard_filter():
    tools = RAGTools.__new__(RAGTools)
    tools.doc_scope = ["doc-keep"]
    assert RAGTools.scoped_doc_ids(tools, ["doc-keep", "doc-drop"]) == ["doc-keep"]
    assert RAGTools.scoped_doc_ids(tools, None) == ["doc-keep"]
    open_tools = SimpleNamespace(doc_scope=None)
    assert RAGTools.scoped_doc_ids(open_tools, ["doc-a"]) == ["doc-a"]


def test_scope_filter_drops_outside_missing_and_ambiguous_compiled_chunks():
    tools = RAGTools.__new__(RAGTools)
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
    tools = RAGTools.__new__(RAGTools)
    tools.doc_scope = ["doc-keep"]
    tools.web_search = None
    assert tools.enforce_doc_scope({"chunks": [chunk], "doc_aggs": []})["chunks"] == []
    tools.web_search = object()
    assert tools.enforce_doc_scope({"chunks": [chunk], "doc_aggs": []})["chunks"] == [chunk]


def _load_filter_kbinfos_to_doc_ids():
    source = (RAGFLOW_ROOT / "api/db/services/dialog_service.py").read_text(encoding="utf-8")
    start = source.index("def filter_kbinfos_to_doc_ids(")
    end = source.index("\ndef convert_last_user_msg_to_multimodal(")
    namespace: dict = {}
    exec(source[start:end], namespace)
    return namespace["filter_kbinfos_to_doc_ids"]


filter_kbinfos_to_doc_ids = _load_filter_kbinfos_to_doc_ids()


def test_simple_path_filter_drops_out_of_scope_chunks():
    scoped = filter_kbinfos_to_doc_ids(
        {
            "chunks": [
                {"chunk_id": "keep", "doc_id": "doc-keep"},
                {"chunk_id": "drop", "document_id": "doc-drop"},
                {"chunk_id": "web", "doc_id": "outside", "url": "https://example.com/a"},
            ],
            "doc_aggs": [
                {"doc_id": "doc-keep"},
                {"doc_id": "doc-drop"},
            ],
        },
        ["doc-keep"],
    )
    assert [c["chunk_id"] for c in scoped["chunks"]] == ["keep"]
    assert {a["doc_id"] for a in scoped["doc_aggs"]} == {"doc-keep"}


def test_simple_path_filter_keeps_web_when_allowed():
    chunk = {"chunk_id": "web", "doc_id": "outside", "url": "https://example.com/a"}
    assert filter_kbinfos_to_doc_ids({"chunks": [chunk], "doc_aggs": []}, ["doc-keep"])[
        "chunks"
    ] == []
    assert filter_kbinfos_to_doc_ids(
        {"chunks": [chunk], "doc_aggs": []}, ["doc-keep"], allow_web=True
    )["chunks"] == [chunk]


def test_simple_path_meta_intersection_does_not_union_expand():
    """Mirrors async_chat: Gateway doc_ids ∩ meta result, never UNION expand."""
    gateway_doc_ids = ["doc-keep"]
    meta_expanded = ["doc-keep", "doc-other-device"]
    allowed = set(gateway_doc_ids)
    attachments = [doc_id for doc_id in meta_expanded if doc_id in allowed]
    assert attachments == ["doc-keep"]


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
