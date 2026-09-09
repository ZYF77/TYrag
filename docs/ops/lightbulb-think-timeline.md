# Lightbulb Thinking timeline (scheme A)

## What to read

With enterprise `grounding_version=1`:

1. **Prompt dialog / lightbulb prompt field**: fixed safe summary only (`_GROUNDING_PROMPT_SUMMARY`). Never restores real system prompt / knowledge / tool I/O.
2. **Thinking fold** (`<think>...</think>` via frontend `replaceThinkToSection`): **structured safe timeline**.
   - L1: outer Thinking fold (existing)
   - L2: each stage is nested `<details class="think-stage">` with safe metadata

## Stage names vs tracing

| Display | Typical source |
|---------|----------------|
| warmup | model_bind |
| scope | metadata_filter |
| understand | refine_multiturn / keyword_analysis / cross_languages |
| retrieval | retrieval / toc_enhance / context_build |
| rerank | retrieval path when rerank configured |
| tool | web_search / knowledge_graph / sql_generation |
| llm | answer_generation |
| citation | reference_metadata |
| Hybrid search / Planner / ... | Agentic bracket logs (think_log) |

Metadata allowlist only: durations, status, counts, enabled/executed, skipReason, toolName, etc. Never prompt/chunk/tool bodies.

## Implementation

- Backend: `rag/advanced_rag/think_timeline.py` collect + render nested details
- `record_timed_rag_stage` mirrors safe stages when timeline active (same names as diagnostics)
- Agentic: `public_think_log_detail` feeds timeline (not a raw "Running the rag tool..." blob)
- Frontend: pure nested HTML details first (`rehype-raw`); no large `replaceThinkToSection` rewrite

## Verify

```bash
pytest ragflow/test/unit_test/rag/advanced_rag/test_think_timeline.py -q
pytest ragflow/test/unit_test/rag/advanced_rag/test_think_log.py -q
pytest ragflow/test/unit_test/api/db/services/test_dialog_service_grounding.py -q -k "timeline or grounding_stream_does_not"
```

Manual: grounding on -> ask a question -> open Thinking -> foldable stages with duration/counts, no raw prompt/knowledge.

## Parallel-work boundary

- Do not revert webhook / document-run callback files
- Diagnostics/tracing: additive mirror only; EAM primary fields unchanged; redact stays on; grounding_version=1 default unchanged
