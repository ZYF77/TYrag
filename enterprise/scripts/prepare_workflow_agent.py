"""Render the checked-in Agent template with an existing chat model id.

Default tenant chat model is VolcEngine 豆包 used in production smoke:
ep-20260310093543-zl952@LLM@VolcEngine

This script never accepts or writes provider/API keys. Configure optional web
providers through the RAGFlow tenant UI or a protected runtime secret.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_DEFAULT_CHAT_LLM_ID = "ep-20260310093543-zl952@LLM@VolcEngine"
_PLACEHOLDER = "__REPLACE_WITH_CHAT_LLM_ID__"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--llm-id",
        default=_DEFAULT_CHAT_LLM_ID,
        help=f"Tenant chat model id (default: {_DEFAULT_CHAT_LLM_ID})",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("enterprise/workflows/enterprise_qa_agent_v1.template.json"),
    )
    args = parser.parse_args()
    llm_id = args.llm_id.strip()
    if not llm_id or any(char in llm_id for char in "\r\n"):
        raise SystemExit("--llm-id must be a non-empty single-line model id")
    if "deepseek" in llm_id.lower():
        raise SystemExit("--llm-id must not be DeepSeek; use VolcEngine 豆包")
    payload = json.loads(args.template.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    # Replace inside an already serialized JSON string using JSON escaping so
    # an unusual (but valid) model id cannot corrupt the generated artifact.
    escaped_llm_id = json.dumps(llm_id, ensure_ascii=False)[1:-1]
    escaped_default = json.dumps(_DEFAULT_CHAT_LLM_ID, ensure_ascii=False)[1:-1]
    if _PLACEHOLDER in serialized:
        serialized = serialized.replace(_PLACEHOLDER, escaped_llm_id)
    elif escaped_default in serialized:
        serialized = serialized.replace(escaped_default, escaped_llm_id)
    else:
        raise SystemExit(
            "template has neither placeholder nor default VolcEngine llm_id to replace"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    print(f"prepared-agent={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
