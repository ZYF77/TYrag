"""Structured safe Thinking timeline for enterprise grounding lightbulb UI.

Renders nested HTML ``<details>`` so the frontend ``replaceThinkToSection``
outer Thinking fold can expand into per-stage metadata without leaking
prompts, chunk bodies, tool I/O, or sensitive question text.

Stage display names align with Gateway/RAG diagnostics tracing where possible
(scope / warmup / understand / retrieval / rerank / tool / llm / citation)
while agentic bracket tags keep their human labels (Hybrid search, Planner…).
"""

from __future__ import annotations

import html
import re
import time
from contextvars import ContextVar, Token
from typing import Any

_MAX_ENTRIES = 64

# Only these metadata keys may appear in the user-visible timeline.
_SAFE_META_KEYS = frozenset(
    {
        "durationMs",
        "status",
        "enabled",
        "executed",
        "skipReason",
        "mode",
        "method",
        "hitCount",
        "chunkCount",
        "retrievedChunkCount",
        "includedChunkCount",
        "fieldCount",
        "languageCount",
        "inputQuestionCount",
        "toolName",
        "toolCount",
        "toolCallCount",
        "toolDurationMsTotal",
        "chatModelConfigured",
        "rerankEnabled",
        "effectiveKnowledgeChars",
        "source",
    }
)

# Internal diagnostics / agentic stage -> tracing-aligned display label.
_STAGE_DISPLAY = {
    "model_bind": "warmup",
    "metadata_filter": "scope",
    "refine_multiturn": "understand",
    "keyword_analysis": "understand",
    "cross_languages": "understand",
    "retrieval": "retrieval",
    "toc_enhance": "retrieval",
    "context_build": "retrieval",
    "rerank": "rerank",
    "web_search": "tool",
    "knowledge_graph": "tool",
    "sql_generation": "tool",
    "answer_generation": "llm",
    "reference_metadata": "citation",
    "ttft": "ttft",
    "citation": "citation",
    "scope": "scope",
    "warmup": "warmup",
    "understand": "understand",
    "tool": "tool",
    "llm": "llm",
}

_AGENTIC_TAG_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")

_TIMELINE: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "public_think_timeline", default=None
)
_TIMELINE_STARTED: ContextVar[float | None] = ContextVar(
    "public_think_timeline_started", default=None
)


def begin_think_timeline() -> Token:
    """Activate a request-scoped public timeline collector."""
    _TIMELINE_STARTED.set(time.perf_counter())
    return _TIMELINE.set([])


def reset_think_timeline(token: Token) -> None:
    try:
        _TIMELINE_STARTED.set(None)
    except Exception:
        pass
    try:
        _TIMELINE.reset(token)
    except Exception:
        pass


def end_think_timeline() -> None:
    """Deactivate the collector without requiring the begin token."""
    try:
        _TIMELINE.set(None)
    except Exception:
        pass
    try:
        _TIMELINE_STARTED.set(None)
    except Exception:
        pass


def think_timeline_active() -> bool:
    try:
        return _TIMELINE.get() is not None
    except Exception:
        return False


def display_stage_name(stage: str) -> str:
    raw = (stage or "").strip()
    if not raw:
        return "stage"
    from rag.advanced_rag.think_log import _STAGE_DESCRIPTIONS
    if raw in _STAGE_DESCRIPTIONS:
        return raw[1:-1].strip()
    return _STAGE_DISPLAY.get(raw, "")


def _safe_meta(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in _SAFE_META_KEYS:
            continue
        if value is None or isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int):
            out[key] = value
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                continue
            out[key] = round(float(value), 3)
        elif isinstance(value, str):
            allowed = {
                "status": {"success", "failed", "skipped", "empty", "started", "info"},
                "mode": {"agentic", "simple", "low", "medium", "high", "ultra"},
                "source": {"ragflow", "agentic", "gateway"},
            }
            if value in allowed.get(key, set()):
                out[key] = value
    return out


def record_think_timeline_stage(
    stage: str,
    *,
    meta: dict[str, Any] | None = None,
    source: str = "ragflow",
) -> None:
    """Append one safe stage entry when the collector is active."""
    try:
        entries = _TIMELINE.get()
        if entries is None:
            return
        if len(entries) >= _MAX_ENTRIES:
            return
        started = _TIMELINE_STARTED.get() or time.perf_counter()
        stage_name = str(stage or "").strip()[:64]
        from rag.advanced_rag.think_log import _STAGE_DESCRIPTIONS
        if stage_name not in _STAGE_DISPLAY and stage_name not in _STAGE_DESCRIPTIONS:
            return
        safe = _safe_meta(meta)
        # Deduplicate identical consecutive stage+status (common for repeated logs).
        fingerprint = (
            stage_name,
            safe.get("status"),
            safe.get("mode"),
            safe.get("toolName"),
            round(float(safe["durationMs"]), 1) if isinstance(safe.get("durationMs"), (int, float)) else None,
        )
        if entries:
            last = entries[-1]
            last_fp = (
                last.get("stage"),
                (last.get("meta") or {}).get("status"),
                (last.get("meta") or {}).get("mode"),
                (last.get("meta") or {}).get("toolName"),
                round(float((last.get("meta") or {}).get("durationMs")), 1)
                if isinstance((last.get("meta") or {}).get("durationMs"), (int, float))
                else None,
            )
            if fingerprint == last_fp:
                return
        entries.append(
            {
                "stage": stage_name,
                "display": display_stage_name(stage_name),
                "atMs": round(max(0.0, (time.perf_counter() - started) * 1000), 3),
                "meta": safe,
                "source": source if source in {"ragflow", "agentic", "gateway"} else "ragflow",
            }
        )
    except Exception:
        # Never let timeline recording break chat.
        pass


def record_think_timeline_from_agentic_log(safe_line: str) -> None:
    """Project a sanitized agentic think line into the timeline."""
    if not safe_line:
        return
    text = safe_line.replace("<br>", "").strip()
    match = _AGENTIC_TAG_RE.match(text)
    if not match:
        record_think_timeline_stage(text[:64], meta={"status": "info"}, source="agentic")
        return
    tag = f"[{match.group(1).strip()}]"
    description = (match.group(2) or "").strip()
    meta: dict[str, Any] = {"status": "success"}
    # Descriptions are static; keep only short known phrases as skipReason-like note.
    if description and len(description) <= 80 and "<" not in description:
        meta["mode"] = "agentic"
    record_think_timeline_stage(tag, meta=meta, source="agentic")


def snapshot_think_timeline() -> list[dict[str, Any]]:
    try:
        entries = _TIMELINE.get()
        if not entries:
            return []
        return [dict(item) for item in entries]
    except Exception:
        return []


def render_think_timeline(entries: list[dict[str, Any]] | None = None) -> str:
    """Render nested ``<details>`` blocks for the Thinking lightbulb."""
    items = entries if entries is not None else snapshot_think_timeline()
    if not items:
        return ""
    blocks: list[str] = [
        "<p><em>Structured safe execution timeline (no prompt / knowledge / tool bodies).</em></p>"
    ]
    for item in items:
        display = display_stage_name(str(item.get("stage") or ""))
        if not display:
            continue
        display = html.escape(display)
        meta = _safe_meta(item.get("meta"))
        duration = meta.get("durationMs")
        status = meta.get("status")
        summary_bits = [f"<strong>{display}</strong>"]
        if isinstance(duration, (int, float)):
            summary_bits.append(f"{float(duration):.1f}ms")
        if status is not None:
            summary_bits.append(html.escape(str(status)))
        summary = " · ".join(summary_bits)
        lines = []
        for key in sorted(meta.keys()):
            if key in {"durationMs", "status"}:
                continue
            val = meta[key]
            if isinstance(val, bool):
                rendered = "true" if val else "false"
            else:
                rendered = html.escape(str(val))
            lines.append(f"<li><code>{html.escape(key)}</code>: {rendered}</li>")
        if isinstance(duration, (int, float)):
            lines.insert(0, f"<li><code>durationMs</code>: {float(duration):.3f}</li>")
        if status is not None:
            lines.insert(0, f"<li><code>status</code>: {html.escape(str(status))}</li>")
        body = (
            f"<ul>\n{chr(10).join(lines)}\n</ul>"
            if lines
            else "<p><em>No additional safe metadata.</em></p>"
        )
        blocks.append(
            f"<details class=\"think-stage\"><summary>{summary}</summary>\n{body}\n</details>"
        )
    return "\n".join(blocks)


def stages_from_flat_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Convert legacy flat ``[Stage] description`` lines into timeline entries."""
    entries: list[dict[str, Any]] = []
    for line in lines or []:
        text = (line or "").replace("<br>", "").strip()
        if not text:
            continue
        match = _AGENTIC_TAG_RE.match(text)
        if match:
            stage = f"[{match.group(1).strip()}]"
        else:
            stage = text[:64]
        if not display_stage_name(stage):
            continue
        entries.append(
            {
                "stage": stage,
                "display": display_stage_name(stage),
                "atMs": 0.0,
                "meta": {"status": "success", "mode": "agentic"},
                "source": "agentic",
            }
        )
    return entries
