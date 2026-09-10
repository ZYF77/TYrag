from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.llm.chat_model import Base, VolcEngineChat
from rag.llm.tool_decorator import tool


class _AsyncStream:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        return self._items()

    async def _items(self):
        for item in self.items:
            yield item


class _CompletionEndpoint:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _response(*, content=None, tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason="stop")
    return _AsyncStream([SimpleNamespace(choices=[choice])])


def _model(endpoint):
    model = VolcEngineChat(
        "test-key",
        "test-model",
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        max_rounds=1,
    )
    model.async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint)
    )
    return model


def _base_model(endpoint):
    model = Base(
        "test-key",
        "test-model",
        "http://127.0.0.1:1/v1",
        max_retries=0,
        max_rounds=1,
    )
    model.async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint)
    )
    return model


@pytest.mark.asyncio
async def test_valid_plhd_is_executed_as_registered_rag_tool():
    called = []

    @tool
    async def rag(question: str) -> str:
        called.append(question)
        return "retrieved answer"

    payload = (
        '<[PLHD20_never_used_test]>'
        '[{"name":"rag","parameters":{"question":"standalone question"}}]'
        '<[PLHD21_never_used_test]>'
    )
    endpoint = _CompletionEndpoint([_response(content=payload)])
    model = _model(endpoint)
    model.bind_tools(tools=[rag])
    model.terminal_tools = {"rag"}

    events = [
        event
        async for event in model.async_chat_streamly_with_tools(
            "", [{"role": "user", "content": "follow-up"}], {}
        )
    ]

    assert called == ["standalone question"]
    assert events[-2] == "retrieved answer"
    assert isinstance(events[-1], int)
    assert all("PLHD" not in str(event) for event in events)
    assert endpoint.calls[0]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_invalid_plhd_retries_required_once_then_returns_protocol_error():
    invalid = (
        '<[PLHD20_never_used_test]>not-json'
        '<[PLHD21_never_used_test]>'
    )
    endpoint = _CompletionEndpoint(
        [_response(content=invalid), _response(content="direct answer")]
    )
    model = _model(endpoint)

    @tool
    async def rag(question: str) -> str:
        return question

    model.bind_tools(tools=[rag])
    events = [
        event
        async for event in model.async_chat_streamly_with_tools(
            "", [{"role": "user", "content": "follow-up"}], {}
        )
    ]

    assert events[-2] == "**ERROR**: RAGFLOW_TOOL_PROTOCOL_INVALID"
    assert isinstance(events[-1], int)
    assert "direct answer" not in events
    assert [call["tool_choice"] for call in endpoint.calls] == [
        "auto",
        "required",
    ]


@pytest.mark.asyncio
async def test_base_provider_keeps_standard_tool_call_path():
    called = []

    @tool
    async def rag(question: str) -> str:
        called.append(question)
        return "standard answer"

    tool_call = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(
            name="rag", arguments='{"question":"standard question"}'
        ),
    )
    delta = SimpleNamespace(
        content=None,
        tool_calls=[tool_call],
        reasoning_content=None,
        reasoning=None,
    )
    endpoint = _CompletionEndpoint(
        [_AsyncStream([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])]
    )
    model = _base_model(endpoint)
    model.bind_tools(tools=[rag])
    model.terminal_tools = {"rag"}

    events = [
        event
        async for event in model.async_chat_streamly_with_tools(
            "", [{"role": "user", "content": "question"}], {}
        )
    ]

    assert called == ["standard question"]
    assert events[-2] == "standard answer"
    assert endpoint.calls[0]["tool_choice"] == "auto"


def test_text_protocol_is_disabled_for_base_path():
    assert Base._supports_text_tool_protocol(object()) is False
    assert VolcEngineChat._supports_text_tool_protocol(object()) is True
