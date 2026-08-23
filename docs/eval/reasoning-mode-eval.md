# Reasoning mode eval (medium / high / ultra)

Same retrieval settings and v9 prompt. Do not retune `top_n` / similarity / rerank in this run.

Identifier Fuse, Numeric Fuse, short retry, and dual-model checks stay off.

## Cases

See [`enterprise/eval/reasoning_mode_cases.json`](../../enterprise/eval/reasoning_mode_cases.json).

## Metrics

| Mode | Accuracy | Hallucination rate | False abstain rate | Citation accuracy | TTFT | Total time |
|---|---|---|---|---|---|---|
| medium | pending live run | pending live run | pending live run | pending live run | pending live run | pending live run |
| high | pending live run | pending live run | pending live run | pending live run | pending live run | pending live run |
| ultra | pending live run | pending live run | pending live run | pending live run | pending live run | pending live run |

Hallucination here means a fabricated equipment / work-order / material identifier, or a numeric business fact not in the cited evidence.

False abstain means the knowledge base has relevant chunks but the answer used the standard no-evidence phrase.

## Follow-up

Re-enable Identifier Guard only if this eval still shows fabricated IDs. Do not restore Numeric Guard from this table alone.

Runner: `python enterprise/scripts/eval_reasoning_modes.py` (needs a live Gateway + EAM JWT; dry-run prints the matrix without calling LLM).
