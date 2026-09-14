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
| `gateway_prepare` | From ASGI request receipt to the trace opening; covers request parsing, lock/scope preparation and run reservation when a run is created |
| `run_started` | Server emits the `run.started` event; its duration is measured from request receipt |
| `stream_first_reasoning` | First non-empty `reasoning.delta`; absent when no reasoning is emitted |
| `stream_first_answer` | First non-empty `answer.delta`; this is the first useful answer-body wait point |
| `http_response` | Response headers and first non-empty response body send times when available; these are server send times, not browser receive times |
| `stream_first_token` | Compatibility metric: first streamed token or think marker (TTFT). Prefer `stream_first_reasoning` / `stream_first_answer` to distinguish the two |
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

- `http_response` is populated in the run trace when the response has started before the run is finalized; the audit record remains the source for transport timing when a JSON response is finalized before its body is sent.
- Per-tool full timelines beyond harness `ResearchToolSession` / text fallback are aggregate-oriented (`toolCallCount`, `toolDurationMsTotal`); tool args/results intentionally omitted.
- List-messages / single-citation projection is not part of the ask-path phase trace.
