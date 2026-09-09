# Inquiry phased diagnostics (`_diagnostics`)

Private, request-scoped timing for one slow ask. Default **off**; no production noise when disabled.

## Enable

Gateway (env or runtime settings):

```bash
ENTERPRISE_RAG_DIAGNOSTICS_ENABLED=true
```

When on, v2 ask (JSON + SSE) starts a Gateway trace and sends `enterprise_diagnostics=true` to RAGFlow so upstream stages merge into the same `_diagnostics` object (runId-matched).

`_diagnostics` is stored on the message run / returned on private console paths. Public EAM payloads strip keys starting with `_` (`_public_run_payload`), so clients do not see the trace unless a diagnostics API is used.

## How to read

Each event has:

| Field | Meaning |
| --- | --- |
| `type` | Event name (Gateway stage or `stage`/`llm`/`context` from RAGFlow) |
| `atMs` | Cumulative ms from Gateway trace start |
| `durationMs` | This stage’s own elapsed ms |
| `data.stage` | Stable stage id when present |
| `sourceAtMs` | Upstream-local cumulative ms (merged RAGFlow events only) |

### Gateway stages (ask path)

| Stage / type | Meaning |
| --- | --- |
| `request` | Trace opened (query truncated; no body dump) |
| `scope` / `gateway_scope` | ACL/context scope compile (`_context_scope`) |
| `attachment_understand` | Upload + image Understand total; **skipped** when no attachments |
| `chat_session` | Session ensure/reuse; `binding=warmup_hit\|ensure_fallback` |
| `upstream_request` / `ragflow_request` | Full RAGFlow completion/stream call |
| `stream_first_token` | SSE: first streamed token/think marker (TTFT). JSON: derived from upstream `llm.ttftMs` when present (`status=derived_from_upstream_llm`) |
| `citation_projection` | `_project_citations` (download tickets / public shape) |
| `outcome` | Final business outcome |

### RAGFlow stages (merged when diagnostics on)

Stable names already emitted via `record_timed_rag_stage`:

| Stage | Meaning |
| --- | --- |
| `embedding` | Query embedding |
| `candidate_search` | Retrieval / candidate pool |
| `rerank` | Rerank (`enabled`/`executed`/`mode`; may be disabled/skipped) |
| `answer_generation` | Final LLM generation |
| `tool` | Agentic/harness tool call (name + running `toolCallCount` / `toolDurationMsTotal`) |
| other | `model_bind`, `metadata_filter`, `refine_multiturn`, `cross_languages`, `keyword_analysis`, `context_build`, `web_search`, … |

LLM events may include `ttftMs` and a logical `stage`.

## Safety

Never put prompt / knowledge / chunk body / tool args / tool results into the trace. Gateway and RAGFlow sinks strip blocked keys.

## Gaps

- JSON true HTTP first-byte is not instrumented at the transport layer; TTFT uses upstream `llm.ttftMs` when available.
- Per-tool full timelines beyond harness `ResearchToolSession` / text fallback are aggregate-oriented (`toolCallCount`, `toolDurationMsTotal`); tool args/results intentionally omitted.
- List-messages / single-citation projection is not part of the ask-path phase trace.
