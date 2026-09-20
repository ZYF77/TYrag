"""Dispatch contract tests, without model, database or customer fixtures.

Run with PYTHONPATH=ragflow pytest --noconftest <this file> when the full
RAGFlow parser/model dependencies are unavailable.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from rag import diagnostics as diag
from rag.workflow_diagnostics import diagnosed_canvas_events, invoke_diagnosed_node
from rag.workflow_diagnostics import bind_deferred_diagnostics


class FutureNode:
    """A previously unknown node type using the ordinary Canvas protocol."""

    def __init__(self, node_id="future-1", deferred=False, failed=False):
        self._id = node_id
        self.values = {}
        self.deferred = deferred
        self.failed = failed

    def invoke(self, **kwargs):
        if self.deferred:
            self.values["content"] = partial(self.generate)
        else:
            self.record()

    async def invoke_async(self, **kwargs):
        await asyncio.sleep(0)
        self.invoke(**kwargs)

    def record(self):
        diag.record_rag_diagnostics("llm", {"modelId": "fixture-model", "durationMs": 3})
        with diag.rag_diagnostics_span("workflow_tool", toolName="future-search"):
            diag.record_rag_diagnostics("retrieval", {"candidateCount": 2})

    async def generate(self):
        self.record()
        yield "synthetic answer"

    def output(self, key=None):
        return self.values.get(key) if key else self.values

    def set_output(self, key, value):
        self.values[key] = value

    def error(self):
        return "secret exception contents" if self.failed else None


class CanvasFixture:
    def __init__(self, pool, nodes=(), async_nodes=True):
        self._thread_pool = pool
        self.nodes = nodes
        self.async_nodes = async_nodes
        self.closed = False

    def get_component_name(self, node_id):
        return "Synthetic node"

    def get_component_type(self, node_id):
        return "FutureComponent"

    async def run(self, **kwargs):
        try:
            for node in self.nodes:
                await invoke_diagnosed_node(self, node, node.invoke, {"query": "secret input"}, self.async_nodes)
                if node.deferred:
                    async for text in node.output("content")():
                        yield {"event": "message", "data": {"content": text}}
                yield {"event": "node_finished", "data": {"component_id": node._id}}
            yield {"event": "workflow_finished", "data": {"usage": {"total_tokens": 3}}}
        finally:
            self.closed = True


@pytest.mark.parametrize("use_async", [False, True])
def test_unknown_nodes_capture_children_and_repeated_executions(use_async):
    async def run():
        with ThreadPoolExecutor(max_workers=2) as pool:
            canvas = CanvasFixture(pool, [FutureNode(), FutureNode()], use_async)
            events = [event async for event in diagnosed_canvas_events(canvas, {}, enabled=True, run_id="run-fixture")]
            assert canvas.closed
        assert diag.snapshot_rag_diagnostics() == {}
        assert diag._CURRENT_SPAN.get() is None
        return events[-1]["data"]["_diagnostics"]

    trace = asyncio.run(run())
    starts = [e["data"] for e in trace["events"] if e["type"] == "workflow_node_started"]
    assert len({e["spanId"] for e in starts}) == 2
    tools = [e["data"] for e in trace["events"] if e["type"] == "workflow_tool_finished"]
    assert {e["parentSpanId"] for e in tools} == {e["spanId"] for e in starts}
    assert all(e["componentType"] == "FutureComponent" for e in tools)
    assert "secret input" not in str(trace)
    assert trace["runId"] == "run-fixture"


def test_parallel_nodes_and_requests_are_isolated():
    async def request(run_id):
        token = diag.begin_rag_diagnostics(True, run_id)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                canvas = CanvasFixture(pool)
                a, b = FutureNode("a"), FutureNode("b")
                await asyncio.gather(*[
                    invoke_diagnosed_node(canvas, node, node.invoke, {}, True) for node in (a, b)
                ])
            return diag.snapshot_rag_diagnostics()
        finally:
            diag.reset_rag_diagnostics(token)

    async def run():
        return await asyncio.gather(request("run-a"), request("run-b"))

    traces = asyncio.run(run())
    assert [t["runId"] for t in traces] == ["run-a", "run-b"]
    span_sets = []
    for trace in traces:
        starts = {e["data"]["spanId"]: e["data"]["componentId"] for e in trace["events"] if e["type"] == "workflow_node_started"}
        tools = [e["data"] for e in trace["events"] if e["type"] == "workflow_tool_finished"]
        assert all(starts[e["parentSpanId"]] == e["componentId"] for e in tools)
        span_sets.append(set(starts))
    assert span_sets[0].isdisjoint(span_sets[1])


def test_deferred_generation_keeps_producer_identity_without_leaking_to_consumer():
    async def run():
        token = diag.begin_rag_diagnostics(True, "run-lazy")
        try:
            with ThreadPoolExecutor() as pool:
                canvas = CanvasFixture(pool)
                node = FutureNode("producer", deferred=True)
                await invoke_diagnosed_node(canvas, node, node.invoke, {}, True)
                with diag.rag_diagnostics_span("workflow_node", componentId="message"):
                    async for chunk in node.output("content")():
                        assert chunk == "synthetic answer"
                        diag.record_rag_diagnostics("consumer", {})
                return diag.snapshot_rag_diagnostics()
        finally:
            diag.reset_rag_diagnostics(token)

    trace = asyncio.run(run())
    llm = next(e for e in trace["events"] if e["type"] == "llm")
    consumer = next(e for e in trace["events"] if e["type"] == "consumer")
    assert llm["data"]["componentId"] == "producer"
    assert consumer["data"]["componentId"] == "message"
    stream = next(e for e in trace["events"] if e["type"] == "workflow_node_stream_finished")
    assert stream["durationMs"] >= 0
    assert stream["data"]["spanId"] == llm["data"]["spanId"]


@pytest.mark.parametrize("enabled,run_id", [(False, "off"), (True, "")])
def test_disabled_or_missing_request_identity_returns_no_diagnostics(enabled, run_id):
    async def run():
        with ThreadPoolExecutor() as pool:
            canvas = CanvasFixture(pool, [FutureNode(deferred=True)])
            return [e async for e in diagnosed_canvas_events(canvas, {}, enabled=enabled, run_id=run_id)]
    assert "_diagnostics" not in str(asyncio.run(run()))


def test_swallowed_node_error_is_visible_without_exception_body():
    async def run():
        with ThreadPoolExecutor() as pool:
            return [e async for e in diagnosed_canvas_events(CanvasFixture(pool, [FutureNode(failed=True)]), {}, enabled=True, run_id="error")]
    trace = asyncio.run(run())[-1]["data"]["_diagnostics"]
    finish = next(e for e in trace["events"] if e["type"] == "workflow_node_finished")
    assert finish["data"]["status"] == "error"
    assert "secret exception" not in str(trace)


def test_exception_emits_partial_trace_and_preserves_failure():
    class BrokenCanvas:
        async def run(self, **kwargs):
            yield {"event": "workflow_started", "data": {}}
            raise ValueError("secret failure body")

    async def run():
        events = []
        with pytest.raises(ValueError, match="secret failure body"):
            async for event in diagnosed_canvas_events(BrokenCanvas(), {}, enabled=True, run_id="failure"):
                events.append(event)
        assert diag.snapshot_rag_diagnostics() == {}
        return events

    events = asyncio.run(run())
    assert events[-1]["event"] == "workflow_diagnostics"
    assert events[-1]["data"]["_diagnostics"]["events"][-1]["data"]["errorType"] == "ValueError"
    assert "secret failure body" not in str(events)


def test_early_close_cleans_up_sink_and_canvas():
    async def run():
        with ThreadPoolExecutor() as pool:
            canvas = CanvasFixture(pool, [FutureNode()])
            iterator = diagnosed_canvas_events(canvas, {}, enabled=True, run_id="close")
            await anext(iterator)
            await iterator.aclose()
            assert canvas.closed
            assert diag.snapshot_rag_diagnostics() == {}
    asyncio.run(run())


def test_cancelled_span_records_terminal_status_and_resets_context():
    async def run():
        token = diag.begin_rag_diagnostics(True, "cancel")
        try:
            with pytest.raises(asyncio.CancelledError):
                with diag.rag_diagnostics_span("workflow_tool", toolName="cancel-test"):
                    raise asyncio.CancelledError()
            assert diag._CURRENT_SPAN.get() is None
            return diag.snapshot_rag_diagnostics()
        finally:
            diag.reset_rag_diagnostics(token)
    trace = asyncio.run(run())
    assert trace["events"][-1]["data"]["status"] == "cancelled"


def test_sync_partial_remains_sync_and_resets_context_on_early_close():
    import inspect

    closed = []

    def generate():
        try:
            diag.record_rag_diagnostics("llm", {"modelId": "sync-fixture"})
            yield "fixture"
            yield "unused"
        finally:
            closed.append(True)

    token = diag.begin_rag_diagnostics(True, "sync-partial")
    try:
        with diag.rag_diagnostics_span("workflow_node", componentId="producer"):
            bound = bind_deferred_diagnostics(partial(generate))
        iterator = bound()
        assert inspect.isgenerator(iterator)
        assert next(iterator) == "fixture"
        assert diag._CURRENT_SPAN.get() is None
        iterator.close()
        assert closed == [True]
        assert diag._CURRENT_SPAN.get() is None
        trace = diag.snapshot_rag_diagnostics()
        assert trace["events"][-1]["data"]["status"] == "cancelled"
        assert trace["events"][-1]["data"]["componentId"] == "producer"
    finally:
        diag.reset_rag_diagnostics(token)
