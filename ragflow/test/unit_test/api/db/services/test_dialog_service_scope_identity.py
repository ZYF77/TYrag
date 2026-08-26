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

import asyncio
from types import SimpleNamespace

from api.db.services import dialog_service


def _collect(async_gen):
    async def _run():
        return [event async for event in async_gen]

    return asyncio.run(_run())


class _FakeModel:
    trace_context = {}

    def __init__(self, answer="型号 XT30D"):
        self.answer = answer
        self.systems = []

    async def async_chat_streamly_delta(self, system, history, _gen_conf, **_kwargs):
        self.systems.append(system)
        yield self.answer

    async def async_chat(self, system, history, _gen_conf, **_kwargs):
        self.systems.append(system)
        return self.answer


class _FakeRetriever:
    async def retrieval(self, *_args, **_kwargs):
        return {
            "chunks": [
                {
                    "doc_id": "doc-1",
                    "content_ltks": "knowledge body",
                    "content_with_weight": "knowledge body",
                    "vector": [0.1],
                }
            ],
            "doc_aggs": [{"doc_id": "doc-1", "doc_name": "doc", "count": 1}],
            "total": 1,
        }

    def retrieval_by_children(self, chunks, _tenant_ids):
        return chunks

    def insert_citations(self, answer, *_args, **_kwargs):
        return answer, set()


def _dialog():
    return SimpleNamespace(
        id="dialog-1",
        kb_ids=["kb-1"],
        tenant_id="tenant-1",
        tenant_llm_id=None,
        llm_id="model-1",
        llm_setting={"temperature": 0.1},
        prompt_type="simple",
        prompt_config={
            "system": "Answer from context. {knowledge}",
            "parameters": [{"key": "knowledge", "optional": False}],
            "quote": False,
            "empty_response": "",
            "refine_multiturn": False,
            "cross_languages": False,
            "keyword": False,
            "toc_enhance": False,
            "tavily_api_key": "",
            "use_kg": False,
            "tts": False,
        },
        meta_data_filter={},
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        top_n=6,
        top_k=1024,
        rerank_id="",
        tenant_rerank_id=None,
    )


def _patch_chat(monkeypatch, model, knowledge=("knowledge body",)):
    monkeypatch.setattr(
        dialog_service,
        "_resolve_dialog_llm_config",
        lambda _dialog: {
            "llm_name": "model-1",
            "model_type": "chat",
            "max_tokens": 8192,
            "llm_factory": "OpenAI",
        },
    )
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", lambda **_kwargs: None)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda _ids: {})
    monkeypatch.setattr(dialog_service.settings, "retriever", _FakeRetriever(), raising=False)
    monkeypatch.setattr(dialog_service, "label_question", lambda _question, _kbs: "")
    monkeypatch.setattr(dialog_service, "kb_prompt", lambda _kbinfos, _max_tokens, **_kwargs: list(knowledge))
    monkeypatch.setattr(
        dialog_service,
        "get_models",
        lambda _dialog, **_kwargs: ([SimpleNamespace(tenant_id="tenant-1")], model, None, model, None),
    )


def test_filter_scope_device_identifiers_keeps_equipment_tokens_only():
    question = "GQ01250024 的合格证上产品型号是什么？"
    filtered = dialog_service._filter_scope_device_identifiers(
        ["GQ01250024", "FA-001", question, "型号", ""],
        reject_values=(question,),
    )
    assert filtered == ["GQ01250024", "FA-001"]


def test_build_scope_identity_block_absent_when_empty():
    assert dialog_service._build_scope_identity_knowledge_block([]) is None
    assert dialog_service._build_scope_identity_knowledge_block(None) is None
    assert dialog_service._build_scope_identity_knowledge_block([""]) is None


def test_build_scope_identity_block_includes_forbid_phrases():
    block = dialog_service._build_scope_identity_knowledge_block(["GQ01250024"])
    assert block is not None
    assert "GQ01250024" in block
    assert "document_metadata.equipment_id" in block
    assert "无法按该编号匹配" in block
    assert "正文未找到该设备号" in block


def test_full_user_question_never_becomes_equipment_token():
    question = "请查询设备 GQ01250024 的合格证型号是什么"
    # Even if the full question leaks into candidates, it must be dropped.
    block = dialog_service._build_scope_identity_knowledge_block(
        [question, "GQ01250024"],
        reject_values=(question,),
    )
    assert block is not None
    assert "GQ01250024" in block
    assert question not in block
    assert "请查询设备" not in block


def test_async_chat_injects_block_from_scope_identifiers(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model)
    question = "GQ01250024 的合格证上产品型号是什么？"

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": question}],
            stream=False,
            scope_identifiers=["GQ01250024"],
            allowed_identifiers=["GQ01250024"],
        )
    )

    assert events
    system = model.systems[0]
    assert "【本轮检索范围设备身份】" in system
    assert "GQ01250024" in system
    assert "无法按该编号匹配" in system
    # Full question must not appear as the equipment-id list entry.
    assert f"设备标识为：{question}" not in system


def test_async_chat_falls_back_to_pre_append_allowed_identifiers(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model)
    question = "这个设备的型号是什么？"

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": question}],
            stream=False,
            allowed_identifiers=["EQ-BOUND-001"],
        )
    )

    assert events
    system = model.systems[0]
    assert "【本轮检索范围设备身份】" in system
    assert "EQ-BOUND-001" in system
    assert f"设备标识为：{question}" not in system


def test_async_chat_skips_identity_block_when_no_identifiers(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model)
    question = "随便问问"

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": question}],
            stream=False,
        )
    )

    assert events
    system = model.systems[0]
    assert "【本轮检索范围设备身份】" not in system
    assert "无法按该编号匹配" not in system
