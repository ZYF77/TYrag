"""Pipeline — unified tool execution dispatcher."""

import time
import logging
from typing import Any

from rag.advanced_rag.harness.types import ToolResult
from rag.advanced_rag.harness.tools.registry import TOOL_REGISTRY

_LOG = logging.getLogger(__name__)

# Tools that retrieve *within* a set of documents. When a routing tool
# (``dataset_navigation_by_tree``) has produced a relevant-document set, these
# inherit it as their ``doc_scope`` unless the caller passed one explicitly, so
# a follow-up search stays within the routed docs instead of re-scanning the KB.
_DOC_SCOPE_CONSUMERS = {"ontology_navigate", "mindmap_navigate", "graph_explore", "hybrid_search", "vector_search", "bm25_search", "structured_query", "dataset_navigation_by_tree"}


class Pipeline:
    """Unified tool execution layer.

    - execute(tool_name, **kwargs): dispatch to registered tool, normalize result
    - available_tools(mode_tools): return LLM-visible tool definitions (compilation-filtered)
    - get_chunks(evidence_ids): retrieve raw chunks for sufficiency cross-check
    - trace: execution history for auditing
    """

    def __init__(self, rag_tools, compilation_map: dict[str, set[str]] | None = None, claim_id: str | None = None):
        self.tools = rag_tools
        self.claim_id = claim_id
        self.compilation_map = compilation_map or {}
        self.trace: list[dict] = []
        # Latest relevant-document set produced by a routing tool this run.
        self._routed_docs: list[str] | None = (list(rag_tools.doc_scope) if getattr(rag_tools, "doc_scope", None) is not None else None)

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """Execute a registered tool by name."""
        tool = TOOL_REGISTRY.get(tool_name)
        if not tool:
            return ToolResult(chunks=[], metadata={}, error=f"Unknown tool: {tool_name}")

        fn = tool.get("fn")
        if not fn:
            return ToolResult(chunks=[], metadata={}, error=f"Tool {tool_name} has no executor")

        # Downstream scoping: a within-document tool inherits the doc IDs a prior
        # router (dataset_navigation_by_tree) produced, unless the caller passed
        # an explicit doc_scope.
        if tool_name in _DOC_SCOPE_CONSUMERS and self._routed_docs is not None and kwargs.get("doc_scope") is None:
            kwargs["doc_scope"] = list(self._routed_docs)

        from rag.advanced_rag.harness.evidence import registry_for
        registry = registry_for(self.tools)
        if tool_name in _DOC_SCOPE_CONSUMERS:
            requested = kwargs.get("doc_scope")
            ceiling = registry.documents
            if requested is not None:
                kwargs["doc_scope"] = [d for d in requested if ceiling is None or d in ceiling]
            elif ceiling is not None:
                kwargs["doc_scope"] = list(ceiling)
            if kwargs.get("doc_scope") == []:
                return ToolResult(chunks=[], docs=[], metadata={})
            if kwargs.get("kb_ids") is not None:
                kwargs["kb_ids"] = [k for k in kwargs["kb_ids"] if k in registry.datasets]
                if not kwargs["kb_ids"]:
                    return ToolResult(chunks=[], docs=[], metadata={})
        start = time.time()
        try:
            raw = await fn(self.tools, **kwargs)
            elapsed = time.time() - start
            self.trace.append({"tool": tool_name, "args": kwargs, "elapsed": elapsed, "success": True})
            result = self._normalize(raw)
            if tool_name == "dataset_navigation_by_tree" and isinstance(raw, list):
                # This tool's contract is list[doc_id], including an empty
                # list. Preserve zero routes instead of treating [] as chunks.
                result = ToolResult(docs=[d for d in raw if isinstance(d, str)], metadata={})
            if hasattr(self.tools, "enforce_doc_scope"):
                before = len(result.chunks)
                scoped = self.tools.enforce_doc_scope(
                    {
                        "chunks": result.chunks,
                        "doc_aggs": result.metadata.get("aggs", []),
                    }
                )
                result.chunks = scoped["chunks"]
                result.metadata["aggs"] = scoped["doc_aggs"]
                dropped = before - len(result.chunks)
                if dropped:
                    _LOG.warning(
                        "Agentic scope dropped tool=%s chunks=%d",
                        tool_name,
                        dropped,
                    )
            # A routing tool (e.g. dataset_navigation_by_tree) yields the relevant
            # document IDs; remember them so the scope-consuming tools above can
            # inherit them on later turns.
            if result.docs is not None:
                if hasattr(self.tools, "scoped_doc_ids"):
                    self._routed_docs = self.tools.scoped_doc_ids(list(result.docs)) or []
                else:
                    self._routed_docs = list(result.docs)
            # Feed the shared citation pool: agent searches go through the
            # pipeline, so without this their evidence never reaches kbinfos and
            # the final answer has nothing to cite.
            self._merge_into_kbinfos(result)
            return result
        except Exception as e:
            elapsed = time.time() - start
            _LOG.exception("Pipeline.execute(%s) failed", tool_name)
            self.trace.append({"tool": tool_name, "args": kwargs, "elapsed": elapsed, "success": False, "error": str(e)})
            return ToolResult(chunks=[], metadata={}, error=str(e))

    def available_tools(self, mode_tools: list[str]) -> list[dict]:
        """Return LLM-visible tool definitions, filtered by compilation availability."""
        names = filter_available_tools(mode_tools, self.compilation_map)
        defs = []
        for name in names:
            tool = TOOL_REGISTRY.get(name)
            if tool and tool.get("function_schema"):
                defs.append(tool["function_schema"])
        return defs

    def get_chunks(self, evidence_ids: list[int]) -> dict[int, dict]:
        from rag.advanced_rag.harness.evidence import registry_for
        registry = registry_for(self.tools)
        return registry.for_claim(self.claim_id, evidence_ids)

    def get_trace(self) -> list[dict]:
        return list(self.trace)

    # ── Private ──

    def _merge_into_kbinfos(self, result: ToolResult) -> None:
        from rag.advanced_rag.harness.evidence import registry_for
        registry = registry_for(self.tools)
        ids = registry.register(result.chunks, self.claim_id)
        result.chunks = [registry.get(eid) for eid in ids]
        result.metadata["evidence_ids"] = ids

    @staticmethod
    def _normalize(raw: Any) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, dict):
            return ToolResult(
                chunks=raw.get("chunks", []),
                docs=raw.get("docs") if "docs" in raw else None,
                metadata={"aggs": raw.get("doc_aggs", []), "answer": raw.get("answer", "")},
            )
        if isinstance(raw, list):
            # A list of doc-id strings is a document-routing result (e.g.
            # dataset_navigation_by_tree); a list of dicts is chunks.
            if raw and all(isinstance(x, str) for x in raw):
                return ToolResult(docs=list(raw), metadata={})
            return ToolResult(chunks=raw, metadata={})
        return ToolResult(chunks=[], metadata={"raw": str(raw)})


def filter_available_tools(tool_names: list[str], compilation_map: dict[str, set[str]]) -> list[str]:
    """Filter tool list by compilation artifact availability."""
    available = []
    for name in tool_names:
        tool = TOOL_REGISTRY.get(name)
        if not tool:
            continue
        if tool.get("requires_compilation"):
            comp_type = tool.get("compilation_type")
            # ``compilation_type`` may name one artifact or several (a tool that
            # reads either one is available when ANY of them is compiled).
            wanted = {comp_type} if isinstance(comp_type, str) else set(comp_type or ())
            if wanted and not any(wanted & comps for comps in compilation_map.values()):
                continue
        available.append(name)
    return available
