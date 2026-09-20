"""Safe instrumentation at the Canvas and tool dispatch boundaries."""

import asyncio
import contextvars
import inspect
from contextlib import aclosing
from functools import partial

from rag.diagnostics import (
    _CURRENT_SPAN,
    _reset_span,
    begin_rag_diagnostics,
    rag_diagnostics_span,
    record_rag_diagnostics,
    reset_rag_diagnostics,
    snapshot_rag_diagnostics,
)


async def invoke_diagnosed_node(canvas, component, sync_fn, arguments, use_async):
    """Single dispatch boundary for all Canvas components, including future types."""
    with rag_diagnostics_span(
        "workflow_node", componentId=component._id,
        componentName=canvas.get_component_name(component._id),
        componentType=canvas.get_component_type(component._id),
    ) as diagnostic:
        if use_async:
            await component.invoke_async(**(arguments or {}))
        else:
            bound_call = partial(sync_fn, **(arguments or {}))
            call_ctx = contextvars.copy_context()
            await asyncio.get_running_loop().run_in_executor(
                canvas._thread_pool, partial(call_ctx.run, bound_call),
            )
        diagnostic["inputCount"] = len(arguments or {})
        diagnostic["outputCount"] = len(component.output() or {})
        if component.error():
            diagnostic["status"] = "error"
        content = component.output("content")
        if isinstance(content, partial):
            component.set_output("content", bind_deferred_diagnostics(content))
            diagnostic["deferred"] = True


async def diagnosed_canvas_events(canvas, run_kwargs, *, enabled=False, run_id=""):
    """Create the sink in the consuming task (including HTTP streaming tasks)."""
    token = begin_rag_diagnostics(enabled, run_id)
    try:
        async with aclosing(canvas.run(**run_kwargs)) as events:
            async for event in events:
                if enabled and event.get("event") in {
                    "node_finished", "workflow_finished", "user_inputs",
                }:
                    snapshot = snapshot_rag_diagnostics()
                    if snapshot:
                        event = {**event, "data": {**event.get("data", {}), "_diagnostics": snapshot}}
                yield event
    except Exception as exc:
        record_rag_diagnostics("workflow_error", {"status": "error", "errorType": type(exc).__name__})
        snapshot = snapshot_rag_diagnostics()
        if snapshot:
            # Preserve the checkpoint for non-stream HTTP error envelopes too.
            try:
                exc.rag_diagnostics = snapshot
            except Exception:
                pass
            yield {"event": "workflow_diagnostics", "data": {"_diagnostics": snapshot}}
        raise
    finally:
        reset_rag_diagnostics(token)


def bind_deferred_diagnostics(value):
    """Keep lazy LLM generation attached to its producer, not its Message consumer.

    Canvas uses functools.partial as the lazy-output protocol. Preserve it, and
    instrument actual iteration separately from the node's preparation time.
    """
    parent = _CURRENT_SPAN.get()
    if not isinstance(value, partial) or not parent:
        return value

    async def async_stream(iterator):
        consumer = _CURRENT_SPAN.get()
        token = _CURRENT_SPAN.set(parent)
        try:
            with rag_diagnostics_span("workflow_node_stream"):
                async with aclosing(iterator):
                    async for chunk in iterator:
                        pause = _CURRENT_SPAN.set(consumer)
                        try:
                            yield chunk
                        finally:
                            _reset_span(pause)
        finally:
            _reset_span(token)

    def sync_stream(iterator):
        consumer = _CURRENT_SPAN.get()
        token = _CURRENT_SPAN.set(parent)
        try:
            with rag_diagnostics_span("workflow_node_stream"):
                try:
                    for chunk in iterator:
                        pause = _CURRENT_SPAN.set(consumer)
                        try:
                            yield chunk
                        finally:
                            _reset_span(pause)
                finally:
                    close = getattr(iterator, "close", None)
                    if close:
                        close()
        finally:
            _reset_span(token)

    def stream():
        token = _CURRENT_SPAN.set(parent)
        try:
            iterator = value()
        finally:
            _reset_span(token)
        return async_stream(iterator) if inspect.isasyncgen(iterator) else sync_stream(iterator)

    return partial(stream)
