"""Execute production API/tool boundaries with synthetic service dependencies.

AST extraction isolates these functions from unrelated RAGFlow model imports;
the function bodies under test are compiled unchanged from production source.
"""
import ast
import asyncio
import copy
import json
import logging
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from timeit import default_timer as timer
from types import SimpleNamespace
from typing import Any

import pytest

from rag import diagnostics as diag
from rag.workflow_diagnostics import diagnosed_canvas_events

ROOT = Path(__file__).resolve().parents[3]


def load_definition(path, name, namespace):
    source = ast.parse((ROOT / path).read_text())
    node = next(node for node in source.body if getattr(node, "name", None) == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class ToolComponent:
    def __init__(self, error=False):
        self.failed = error

    async def invoke_async(self, **kwargs):
        diag.record_rag_diagnostics("retrieval", {"candidateCount": 1})
        return "private tool result"

    def error(self):
        return "private error" if self.failed else None


class MCPSession:
    def tool_call(self, name, arguments, request_timeout):
        return {"isError": True, "content": "private MCP error"}


class MCPBinding:
    def __init__(self):
        self.session = MCPSession()
        self.original_name = "remote-tool"


@pytest.mark.parametrize("tool", [ToolComponent(), ToolComponent(True), MCPSession(), MCPBinding()])
def test_actual_tool_dispatch_covers_builtin_mcp_and_returned_errors(tool, caplog):
    async def thread_pool_exec(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    cls = load_definition("agent/tools/base.py", "LLMToolPluginCallSession", {
        "ToolCallSession": object, "partial": partial, "Any": Any,
        "asyncio": asyncio, "logging": logging, "Mapping": Mapping, "timer": timer,
        "MCPToolBinding": MCPBinding, "MCPToolCallSession": MCPSession,
        "ComponentBase": ToolComponent, "thread_pool_exec": thread_pool_exec,
    })
    calls = []

    async def run():
        token = diag.begin_rag_diagnostics(True, "tool-contract")
        try:
            session = cls({"new-tool": tool}, lambda *args, **kwargs: calls.append(args))
            with diag.rag_diagnostics_span("workflow_node", componentId="agent-1"):
                await session.tool_call_async("new-tool", {"credential": "private tool argument"})
            return diag.snapshot_rag_diagnostics()
        finally:
            diag.reset_rag_diagnostics(token)

    with caplog.at_level(logging.INFO):
        trace = asyncio.run(run())
    finish = next(e for e in trace["events"] if e["type"] == "workflow_tool_finished")
    assert finish["data"]["status"] == ("success" if isinstance(tool, ToolComponent) and not tool.failed else "error")
    assert finish["data"]["parentSpanId"]
    assert finish["durationMs"] >= 0
    assert len(calls) == 1
    assert "private" not in str(trace)
    assert "private" not in caplog.text


class APICanvas:
    def __str__(self):
        return "{}"

    def cancel_task(self):
        pass

    async def run(self, **kwargs):
        with diag.rag_diagnostics_span("workflow_node", componentId="node-1", componentType="FutureType"):
            diag.record_rag_diagnostics("llm", {"modelId": "fixture"})
        yield {"event": "message", "data": {"content": "fixture answer"}}
        yield {"event": "node_finished", "data": {"component_id": "node-1", "outputs": {}}}
        yield {"event": "workflow_finished", "data": {"usage": {"total_tokens": 1}, "outputs": "private output"}}


def test_existing_session_api_preserves_private_trace_on_terminal_frame():
    async def completion(**kwargs):
        async for event in diagnosed_canvas_events(APICanvas(), {}, enabled=True, run_id="api-contract"):
            yield "data:" + json.dumps(event)

    iterator = load_definition("api/apps/restful_apis/agent_api.py", "_iter_session_completion_events", {
        "agent_completion": completion, "json": json, "copy": copy, "logging": logging,
    })

    async def run():
        return [event async for event in iterator("tenant-fixture", "agent-fixture", {}, True)]

    events = asyncio.run(run())
    terminal = events[-1]["data"]
    assert events[-1]["event"] == "workflow_finished"
    assert terminal["_diagnostics"]["runId"] == "api-contract"
    assert terminal["usage"] == {"total_tokens": 1}
    assert "outputs" not in terminal


def test_existing_session_failure_keeps_trace_in_error_envelope():
    async def completion(**kwargs):
        error = ValueError("private failure")
        error.rag_diagnostics = {"runId": "failed-run", "events": []}
        raise error
        yield  # Make this a failing async iterator, as the completion API is.

    iterator = load_definition("api/apps/restful_apis/agent_api.py", "_iter_session_completion_events", {
        "agent_completion": completion, "json": json, "copy": copy, "logging": logging,
    })

    async def run():
        return [event async for event in iterator("tenant", "agent", {}, True)]

    events = asyncio.run(run())
    assert events[-1]["code"] == 500
    assert events[-1]["data"]["_diagnostics"]["runId"] == "failed-run"
    assert "private failure" not in str(events)


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("failed", [True, False])
def test_workflow_session_api_stream_and_json_return_trace(stream, failed):
    async def thread_pool_exec(fn, *args):
        return fn(*args)

    class FailedCanvas(APICanvas):
        async def run(self, **kwargs):
            async for event in super().run(**kwargs):
                if event["event"] != "workflow_finished":
                    yield event
            raise ValueError("private error contents")

    canvas = FailedCanvas() if failed else APICanvas()
    api = load_definition("api/apps/restful_apis/agent_api.py", "_run_workflow_session", {
        "request": SimpleNamespace(headers={"X-Request-ID": "api-fixture"}),
        "CanvasReplicaService": SimpleNamespace(commit_after_run=lambda **kwargs: True),
        "API4ConversationService": SimpleNamespace(append_message=lambda *args: None),
        "_normalize_agent_reference_entry": lambda value: value,
        "get_uuid": lambda: "fixture-id", "thread_pool_exec": thread_pool_exec,
        "_build_sse_response": lambda generator: generator,
        "get_result": lambda **kwargs: kwargs, "_canvas_json_default": str,
        "json": json, "copy": copy, "time": SimpleNamespace(time=lambda: 0), "logging": logging,
    })

    async def run():
        result = await api("tenant", "agent", {}, canvas, "query", [], {}, "user", "session", {}, "title", "workflow", True, stream)
        if stream:
            frames = [json.loads(event[5:]) async for event in result if event != "data:[DONE]\n\n"]
            if failed:
                assert frames[-1]["code"] == 500
                assert "private error contents" not in str(frames[-1])
            return frames[-1]["data"]
        if failed:
            assert result["code"] == 500
            assert "private error contents" not in str(result)
            return result["data"]
        return result["data"]["data"]

    result = asyncio.run(run())
    assert result["_diagnostics"]["runId"] == "api-fixture"
    assert any(e["type"] == "llm" for e in result["_diagnostics"]["events"])
