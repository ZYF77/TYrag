#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _load_graph_module():
    """Load agentic_rag_graph without importing package __init__ (heavy deps)."""
    path = (
        Path(__file__).resolve().parents[4]
        / "rag"
        / "advanced_rag"
        / "agentic_rag_graph.py"
    )
    # File: ragflow/test/unit_test/rag/advanced_rag/test_*.py
    # parents[4] == ragflow/
    name = "agentic_rag_graph_under_test"
    if name in sys.modules:
        return sys.modules[name]

    def _ensure_stubs():
        if "langgraph" not in sys.modules:
            lg = types.ModuleType("langgraph")
            lg_graph = types.ModuleType("langgraph.graph")
            lg_graph.END = "END"
            lg_graph.START = "START"

            class _StateGraph:
                def __init__(self, *_a, **_k):
                    pass

                def add_node(self, *_a, **_k):
                    return None

                def add_edge(self, *_a, **_k):
                    return None

                def compile(self, *_a, **_k):
                    return None

            lg_graph.StateGraph = _StateGraph
            sys.modules["langgraph"] = lg
            sys.modules["langgraph.graph"] = lg_graph
        if "rag.prompts.generator" not in sys.modules:
            if "rag" not in sys.modules:
                sys.modules["rag"] = types.ModuleType("rag")
            if "rag.prompts" not in sys.modules:
                sys.modules["rag.prompts"] = types.ModuleType("rag.prompts")
            gen = types.ModuleType("rag.prompts.generator")
            gen.form_message = lambda *a, **k: []
            gen.kb_prompt = lambda *a, **k: []
            gen.message_fit_in = lambda *a, **k: (0, [])
            sys.modules["rag.prompts.generator"] = gen

    _ensure_stubs()
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError:
        _ensure_stubs()
        spec.loader.exec_module(mod)
    return mod


agentic_rag_graph = _load_graph_module()


def test_formalize_evidence_prepends_identity_block(monkeypatch):
    chunks = [
        {
            "doc_id": "doc-1",
            "kb_id": "kb-1",
            "content_with_weight": "产品型号：XT30D",
            "docnm_kwd": "合格证.pdf",
        }
    ]
    kbinfos = {"chunks": chunks, "doc_aggs": []}
    tools = SimpleNamespace(
        scope_identifiers=["GQ01250024"],
        chat_mdl=SimpleNamespace(max_length=8192),
    )

    enrich_calls = []

    def _fake_enrich(chunk_list, fields):
        enrich_calls.append((chunk_list, set(fields)))
        for ck in chunk_list:
            ck["document_metadata"] = {
                "equipment_id": "GQ01250024",
                "fixed_asset_no": "FA-001",
            }

    # Ensure package exists for patch target before monkeypatch resolve.
    import api.utils.reference_metadata_utils as rmu

    monkeypatch.setattr(rmu, "enrich_chunks_with_document_metadata", _fake_enrich)
    monkeypatch.setattr(
        agentic_rag_graph,
        "kb_prompt",
        lambda _kbinfos, _max_tokens, **_kwargs: ["ID: 0\n└── Content:\n产品型号：XT30D"],
    )

    evidence = agentic_rag_graph._build_formalize_evidence_text(tools, kbinfos)

    assert evidence.startswith("【本轮检索范围设备身份】")
    assert "GQ01250024" in evidence
    assert "无法按该编号匹配" in evidence
    assert "产品型号：XT30D" in evidence
    assert enrich_calls and enrich_calls[0][1] == {"equipment_id", "fixed_asset_no"}
    assert chunks[0]["document_metadata"]["equipment_id"] == "GQ01250024"


def test_formalize_evidence_skips_identity_without_scope(monkeypatch):
    kbinfos = {
        "chunks": [{"doc_id": "doc-1", "content_with_weight": "body"}],
        "doc_aggs": [],
    }
    tools = SimpleNamespace(
        scope_identifiers=[],
        chat_mdl=SimpleNamespace(max_length=8192),
    )
    enrich_called = {"n": 0}

    def _fake_enrich(*_a, **_k):
        enrich_called["n"] += 1

    import api.utils.reference_metadata_utils as rmu

    monkeypatch.setattr(rmu, "enrich_chunks_with_document_metadata", _fake_enrich)
    monkeypatch.setattr(
        agentic_rag_graph,
        "kb_prompt",
        lambda _kbinfos, _max_tokens, **_kwargs: ["ID: 0\n└── Content:\nbody"],
    )

    evidence = agentic_rag_graph._build_formalize_evidence_text(tools, kbinfos)

    assert "【本轮检索范围设备身份】" not in evidence
    assert enrich_called["n"] == 0
    assert evidence == "ID: 0\n└── Content:\nbody"


def test_formalize_evidence_joins_blocks_not_list_repr(monkeypatch):
    tools = SimpleNamespace(
        scope_identifiers=["EQ-1"],
        chat_mdl=SimpleNamespace(max_length=8192),
    )
    import api.utils.reference_metadata_utils as rmu

    monkeypatch.setattr(rmu, "enrich_chunks_with_document_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        agentic_rag_graph,
        "kb_prompt",
        lambda *_a, **_k: ["block-a", "block-b"],
    )

    evidence = agentic_rag_graph._build_formalize_evidence_text(
        tools, {"chunks": [{"doc_id": "d1"}], "doc_aggs": []}
    )

    assert "['block-a'" not in evidence
    assert "block-a\nblock-b" in evidence
