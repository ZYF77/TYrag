import asyncio
import logging
from types import SimpleNamespace

from api.db.services import dialog_service


from rag.grounding.guard import STANDARD_ABSTAIN_ANSWER


def _collect(async_gen):
    async def _run():
        return [event async for event in async_gen]

    return asyncio.run(_run())


def test_effective_knowledge_extracts_complete_start_only_and_missing_start():
    start, end = dialog_service._grounding_markers("knowledge")

    assert dialog_service._extract_effective_knowledge(f"prefix{start}knowledge{end}suffix", start, end) == "knowledge"
    assert dialog_service._extract_effective_knowledge(f"prefix{start}knowledge", start, end) == "knowledge"
    assert dialog_service._extract_effective_knowledge(f"prefix knowledge {end}", start, end) == ""


def test_grounding_markers_regenerate_on_full_marker_collision(monkeypatch):
    first = SimpleNamespace(hex="first")
    second = SimpleNamespace(hex="second")
    monkeypatch.setattr(dialog_service.uuid, "uuid4", iter((first, second)).__next__)

    start, end = dialog_service._grounding_markers("<GROUNDING_START:first>")

    assert start == "<GROUNDING_START:second>"
    assert end == "<GROUNDING_END:second>"


def test_grounding_request_flag_rejects_non_int_one():
    assert dialog_service._grounding_requested(None) is False
    assert dialog_service._grounding_requested("1") is False
    assert dialog_service._grounding_requested(True) is False
    assert dialog_service._grounding_requested(1) is True


class _FakeModel:
    trace_context = {}

    def __init__(self, answer="A safe answer with enough tokens for streaming", retry_answer=None):
        self.answer = answer
        self.retry_answer = retry_answer
        self.systems = []
        self.histories = []
        self.stream_calls = 0
        self.chat_calls = 0

    async def async_chat_streamly_delta(self, system, history, _gen_conf, **_kwargs):
        self.stream_calls += 1
        self.systems.append(system)
        self.histories.append(history)
        yield self.answer

    async def async_chat(self, system, history, _gen_conf, **_kwargs):
        self.chat_calls += 1
        self.systems.append(system)
        self.histories.append(history)
        if self.stream_calls and self.retry_answer is not None:
            return self.retry_answer
        if self.chat_calls > 1 and self.retry_answer is not None:
            return self.retry_answer
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


def _dialog(*, empty_response=""):
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
            "empty_response": empty_response,
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


def _patch_chat(monkeypatch, model, knowledge=("knowledge body",), max_tokens=8192):
    retriever = _FakeRetriever()
    monkeypatch.setattr(
        dialog_service,
        "_resolve_dialog_llm_config",
        lambda _dialog: {"llm_name": "model-1", "model_type": "chat", "max_tokens": max_tokens, "llm_factory": "OpenAI"},
    )
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", lambda **_kwargs: None)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda _ids: {})
    monkeypatch.setattr(dialog_service.settings, "retriever", retriever, raising=False)
    monkeypatch.setattr(dialog_service, "label_question", lambda _question, _kbs: "")
    monkeypatch.setattr(dialog_service, "kb_prompt", lambda _kbinfos, _max_tokens, **_kwargs: list(knowledge))
    monkeypatch.setattr(dialog_service, "get_models", lambda _dialog, **_kwargs: ([SimpleNamespace(tenant_id="tenant-1")], model, None, model, None))
    return retriever


def test_grounding_stream_does_not_yield_candidate_tokens(monkeypatch):
    monkeypatch.setattr(dialog_service, "_IDENTIFIER_NUMERIC_FUSE_ENABLED", True)
    model = _FakeModel("设备 EQ-104 压力 2 MPa")
    _patch_chat(monkeypatch, model, knowledge=("设备编号 EQ-104，额定压力 2 MPa。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "question body"}],
            stream=True,
            grounding_version=1,
        )
    )

    assert all("grounding" not in event for event in events)
    assert all("effectiveKnowledge" not in event for event in events)
    non_final = [event for event in events if not event.get("final")]
    assert non_final == []
    final = [event for event in events if event.get("final")]
    assert len(final) == 1
    assert "EQ-104" in final[0]["answer"]
    assert "<GROUNDING_START:" in model.systems[0]
    assert "<GROUNDING_END:" in model.systems[0]


def test_grounding_stream_yields_candidate_tokens_when_fuse_disabled(monkeypatch):
    assert dialog_service._IDENTIFIER_NUMERIC_FUSE_ENABLED is False
    model = _FakeModel("设备 EQ-104 压力 2 MPa")
    _patch_chat(monkeypatch, model, knowledge=("设备编号 EQ-104，额定压力 2 MPa。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "question body"}],
            stream=True,
            grounding_version=1,
        )
    )

    non_final = [event for event in events if not event.get("final")]
    assert any("EQ-104" in str(event.get("answer") or "") for event in non_final)
    assert events[-1].get("final") is True


def test_use_simple_chat_only_when_reasoning_absent():
    assert dialog_service._use_simple_chat({}, {}) is True
    assert dialog_service._use_simple_chat({"reasoning": 0}, {}) is True
    assert dialog_service._use_simple_chat({}, {"reasoning": 3}) is False
    assert dialog_service._use_simple_chat({"reasoning": 2}, {}) is False


def test_grounding_guard_disabled_by_default_keeps_candidate(monkeypatch):
    assert dialog_service._IDENTIFIER_NUMERIC_FUSE_ENABLED is False
    model = _FakeModel("<think>candidate</think>工单 WO-99999 完成率 20%")
    _patch_chat(monkeypatch, model, knowledge=("发票和收据各一份。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "维修几次"}],
            stream=True,
            grounding_version=1,
        )
    )

    assert "WO-99999" in events[-1]["answer"]
    assert "20%" in events[-1]["answer"]
    assert model.stream_calls == 1
    assert model.chat_calls == 0


def test_grounding_guard_fail_replaces_candidate_before_yield(monkeypatch):
    monkeypatch.setattr(dialog_service, "_IDENTIFIER_NUMERIC_FUSE_ENABLED", True)
    model = _FakeModel("<think>candidate</think>工单 WO-99999 完成率 20%")
    _patch_chat(monkeypatch, model, knowledge=("发票和收据各一份。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "维修几次"}],
            stream=True,
            grounding_version=1,
        )
    )

    answers = " ".join(str(event.get("answer") or "") for event in events)
    assert "WO-99999" not in answers
    assert "20%" not in answers
    assert "candidate" not in answers
    assert events[-1]["answer"] == STANDARD_ABSTAIN_ANSWER
    assert events[-1]["reference"].get("chunks") == []
    assert all(not event.get("answer") for event in events if not event.get("final"))
    assert model.stream_calls == 1
    assert model.chat_calls == 0


def test_grounding_guard_short_retry_recovers_grounded_answer(monkeypatch):
    monkeypatch.setattr(dialog_service, "_IDENTIFIER_NUMERIC_FUSE_ENABLED", True)
    model = _FakeModel(
        "工单 WO-99999 完成率 20%",
        retry_answer="现有发票和收据各一份。[ID:0]",
    )
    _patch_chat(monkeypatch, model, knowledge=("发票和收据各一份。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "有哪些资料"}],
            stream=False,
            grounding_version=1,
        )
    )

    # Identifier mismatch must not trigger short numeric retry.
    assert model.chat_calls == 1
    assert events[-1]["answer"] == STANDARD_ABSTAIN_ANSWER


def test_grounding_guard_short_retry_on_numeric_only_recovers(monkeypatch):
    monkeypatch.setattr(dialog_service, "_IDENTIFIER_NUMERIC_FUSE_ENABLED", True)
    model = _FakeModel(
        "现有发票和收据，共找到 6 条片段。[ID:0]",
        retry_answer="现有发票和收据。[ID:0]",
    )
    _patch_chat(monkeypatch, model, knowledge=("发票和收据各一份。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "有哪些资料"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert model.chat_calls == 1
    assert "发票" in events[-1]["answer"]
    assert events[-1]["answer"] != STANDARD_ABSTAIN_ANSWER


def test_grounding_guard_short_retry_still_abstains_when_retry_ungrounded(monkeypatch):
    monkeypatch.setattr(dialog_service, "_IDENTIFIER_NUMERIC_FUSE_ENABLED", True)
    model = _FakeModel(
        "现有发票，统计完成率约 35%",
        retry_answer="统计完成率约 35%",
    )
    _patch_chat(monkeypatch, model, knowledge=("发票和收据各一份。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "有哪些资料"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert model.chat_calls == 2
    assert events[-1]["answer"] == STANDARD_ABSTAIN_ANSWER
    assert events[-1]["reference"].get("chunks") == []


def test_grounding_kpa_mpa_passes_and_keeps_answer(monkeypatch):
    model = _FakeModel("压力 2 MPa")
    _patch_chat(monkeypatch, model, knowledge=("额定压力 2000 kPa。",))

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "压力是多少"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert events[-1]["answer"] == "压力 2 MPa"
    assert "grounding" not in events[-1]


def test_grounding_non_stream_and_empty_response_are_terminal_only(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model, knowledge=())

    events = _collect(
        dialog_service.async_chat(
            _dialog(empty_response="NO EVIDENCE"),
            [{"role": "user", "content": "question body"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert len(events) == 1
    assert events[0]["final"] is True
    assert events[0]["answer"] == STANDARD_ABSTAIN_ANSWER
    assert events[0]["reference"].get("chunks") == []
    assert "grounding" not in events[0]


def test_grounding_prompt_fit_drops_only_knowledge_tail(monkeypatch):
    model = _FakeModel()
    knowledge = ("KEEP " * 30, "DROP SECOND " * 30, "DROP LAST " * 30)
    _patch_chat(monkeypatch, model, knowledge=knowledge, max_tokens=120)
    monkeypatch.setattr(dialog_service.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    dialog = _dialog()
    dialog.prompt_config["system"] = ("rule " * 80) + "{knowledge}"

    events = _collect(
        dialog_service.async_chat(
            dialog,
            [{"role": "user", "content": "question " * 50}],
            stream=False,
            grounding_version=1,
        )
    )

    assert len(model.systems) == 1
    assert "<GROUNDING_START:fixed>" in model.systems[0]
    assert "KEEP KEEP KEEP" in model.systems[0]
    assert knowledge[-1] not in model.systems[0]
    extracted = dialog_service._extract_effective_knowledge(
        model.systems[0],
        "<GROUNDING_START:fixed>",
        "<GROUNDING_END:fixed>",
    )
    assert extracted
    assert "grounding" not in events[-1]


def test_grounding_prompt_fit_rejects_without_calling_model_when_one_block_cannot_fit(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model, knowledge=("knowledge body",), max_tokens=40)
    dialog = _dialog(empty_response="NO EVIDENCE")
    dialog.prompt_config["system"] = ("mandatory rule " * 80) + "{knowledge}"

    events = _collect(
        dialog_service.async_chat(
            dialog,
            [{"role": "user", "content": "question body"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert model.systems == []
    assert events[-1]["answer"] == STANDARD_ABSTAIN_ANSWER
    assert "grounding" not in events[-1]


def test_grounding_does_not_change_unversioned_payload(monkeypatch):
    model = _FakeModel()
    _patch_chat(monkeypatch, model)

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "question body"}],
            stream=False,
        )
    )

    assert events
    assert all("grounding" not in event for event in events)
    assert len(events) == 1
    assert len(model.systems) == 1


def test_unversioned_stream_still_yields_candidate_tokens(monkeypatch):
    model = _FakeModel("streamed candidate WO-99999")
    _patch_chat(monkeypatch, model)

    events = _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "question body"}],
            stream=True,
        )
    )

    assert any(event.get("answer") == "streamed candidate WO-99999" for event in events if not event.get("final"))
    assert all("grounding" not in event for event in events)


def test_grounding_solo_path_does_not_expose_effective_knowledge(monkeypatch):
    model = _FakeModel()
    bundle_calls = []

    monkeypatch.setattr(
        dialog_service,
        "_resolve_dialog_llm_config",
        lambda _dialog: {"llm_name": "model-1", "model_type": "chat", "max_tokens": 8192, "llm_factory": "OpenAI"},
    )
    monkeypatch.setattr(
        dialog_service,
        "LLMBundle",
        lambda tenant_id, config, **kwargs: (bundle_calls.append((tenant_id, config, kwargs)) or model),
    )
    dialog = _dialog()
    dialog.kb_ids = []
    dialog.prompt_config["tts"] = True

    events = _collect(
        dialog_service.async_chat(
            dialog,
            [{"role": "user", "content": "question body"}],
            stream=False,
            grounding_version=1,
        )
    )

    assert "grounding" not in events[0]
    assert bundle_calls[0][2]["disable_langfuse"] is True
    assert len(bundle_calls) == 1


def test_grounding_request_does_not_log_sensitive_prompt_or_answer(monkeypatch, caplog):
    model = _FakeModel(answer="MODEL OUTPUT BODY")
    _patch_chat(monkeypatch, model)
    caplog.set_level(logging.DEBUG)

    _collect(
        dialog_service.async_chat(
            _dialog(),
            [{"role": "user", "content": "QUESTION BODY"}],
            stream=False,
            grounding_version=1,
        )
    )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "QUESTION BODY" not in logs
    assert "knowledge body" not in logs
    assert "MODEL OUTPUT BODY" not in logs
    assert "retrieved_knowledge_count=" in logs
    assert "included_knowledge_count=" in logs
    assert "effective_knowledge_length=" in logs
    assert "answer_length=" in logs
    assert "contains_empty_response=" in logs


def test_llm_bundle_can_disable_langfuse_for_grounding(monkeypatch):
    from api.db.services import llm_service
    from api.db.services import tenant_llm_service

    calls = []
    model_calls = []
    monkeypatch.setattr(
        tenant_llm_service.TenantLLMService,
        "model_instance",
        lambda *_args, **kwargs: (model_calls.append(kwargs) or SimpleNamespace()),
    )
    monkeypatch.setattr(tenant_llm_service.TenantLangfuseService, "filter_by_tenant", lambda **_kwargs: calls.append(True))

    bundle = llm_service.LLMBundle(
        "tenant-1",
        {"llm_name": "model-1", "model_type": "chat"},
        disable_langfuse=True,
    )

    assert bundle.langfuse is None
    assert model_calls[0]["disable_content_logging"] is True
    assert calls == []


def test_redacted_model_history_log_keeps_only_count(caplog):
    from rag.llm.chat_model import _log_history

    caplog.set_level(logging.INFO)
    _log_history([{"role": "user", "content": "SENSITIVE BODY"}], redact=True)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "message_count=1" in logs
    assert "SENSITIVE BODY" not in logs


def test_rag_agent_grounding_without_reasoning_still_uses_async_chat(monkeypatch):
    async_chat_calls = []

    async def fake_async_chat(*_args, **kwargs):
        async_chat_calls.append(kwargs)
        yield {"answer": "simple", "final": True}

    monkeypatch.setattr(dialog_service, "async_chat", fake_async_chat)
    events = _collect(
        dialog_service.rag_agent(
            _dialog(),
            [{"role": "user", "content": "q"}],
            stream=True,
            grounding_version=1,
        )
    )
    assert async_chat_calls
    assert events[-1]["answer"] == "simple"


def test_rag_agent_grounding_with_reasoning_uses_agentic_path(monkeypatch):
    async_chat_calls = []
    rag_tools_kwargs = []
    get_models_kwargs = []

    async def fake_async_chat(*_args, **kwargs):
        async_chat_calls.append(kwargs)
        yield {"answer": "simple", "final": True}

    class FakeRAGTools:
        def __init__(self, *_args, **kwargs):
            rag_tools_kwargs.append(kwargs)
            self.kbinfos = {"chunks": [], "doc_aggs": []}
            self.tools = []

        def sys_prompt(self):
            return "sys"

    class FakeChat:
        mdl = SimpleNamespace()

        def bind_tools(self, *_args, **_kwargs):
            return None

        async def async_chat_streamly_delta(self, *_args, **_kwargs):
            yield "档位答案"

        async def async_chat(self, *_args, **_kwargs):
            return "档位答案"

    monkeypatch.setattr(dialog_service, "async_chat", fake_async_chat)
    monkeypatch.setattr(
        dialog_service,
        "get_models",
        lambda _dialog, **kwargs: (
            get_models_kwargs.append(kwargs) or ([], None, None, FakeChat(), None)
        ),
    )
    monkeypatch.setattr(dialog_service, "RAGTools", FakeRAGTools)
    monkeypatch.setattr(dialog_service, "_should_use_web_search", lambda *_a, **_k: False)

    events = _collect(
        dialog_service.rag_agent(
            _dialog(),
            [{"role": "user", "content": "question body"}],
            stream=True,
            grounding_version=1,
            reasoning=3,
            doc_ids="doc-keep,doc-drop",
            internet=False,
        )
    )

    assert async_chat_calls == []
    assert get_models_kwargs[0].get("disable_langfuse") is True
    assert rag_tools_kwargs[0]["thinking_mode"] == "high"
    assert rag_tools_kwargs[0]["doc_scope"] == ["doc-keep", "doc-drop"]
    assert rag_tools_kwargs[0]["web_search"] is None
    assert any("档位答案" in str(event.get("answer") or "") for event in events)
    assert any(not event.get("final") and event.get("answer") == "档位答案" for event in events)
