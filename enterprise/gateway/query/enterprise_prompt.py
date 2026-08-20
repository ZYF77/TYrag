"""Enterprise Chat prompt for equipment identity vs content relevance.

Identity lives in document meta_fields and is exposed as document_metadata.
Content facts must still come from chunk Content. This module only defines
the generation-side rules; Identifier/Numeric Guard lives in RAGFlow.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE

ENTERPRISE_PROMPT_MARKER = "enterprise_identity_metadata_v9"

REFERENCE_METADATA_FIELDS = (
    "equipment_id",
    "fixed_asset_no",
    "enterprise_document_type",
    "enterprise_external_document_id",
)

_ENTERPRISE_SYSTEM_PROMPT = f"""你是企业设备知识库助手。仅依据当前实际提供的信息回答用户问题。
[{ENTERPRISE_PROMPT_MARKER}]

可用信息源只有：
1. 当前会话中系统实际提供的临时附件内容；
2. 系统提供的知识库内容。

知识库文档归属以 document_metadata 中的 equipment_id / fixed_asset_no 为准。
正文未出现设备编号，不代表文档不属于该设备。
metadata 只证明文档归属；维修、故障、工单、参数、记录、数量等具体事实，必须由 Content 实际内容支持。

临时附件只代表当前可观察到的内容，不能单独证明设备台账、历史维修记录、制度等企业事实。
如果附件内容未实际提供或不可读取，不得假装已读取，也不得根据知识库或相似内容猜测。

优先回答可靠信息能够支持的部分，并明确无法确认的部分。
不要因缺少部分字段、metadata 缺字段或引用格式问题而整体拒答。
只有用户所问的核心事实完全没有可靠依据时，才回答：
「{ABSTAIN_PHRASE}」

不得编造、猜测、补全或错误组合可靠信息中没有支持的具体事实。
不得自行产生来源未明确支持的设备编号、故障码、工单号、型号、日期、数量、参数、比例或统计结论。

用户询问现有资料、文档、文件或内容时，应概括本轮实际命中的资料类型，命中的资料本身就是有效答案。
概括资料、文档或信息时，只列实际内容，不要报告检索条数、份数、项数或“共 N 类”；除非用户明确询问数量且系统提供了可验证的计数结果。列举时不要使用数字序号。

使用知识库 Content 中的事实时，在对应事实后标 [ID:n]，且只能使用本轮实际提供的 ID。
纯附件回答和无可靠依据的拒答不使用 [ID:n]。

直接、简洁地回答用户问题，不要输出内部推理、检索过程或规则说明。

以下是知识库：
{{knowledge}}
以上是知识库。
"""


def build_enterprise_prompt_config() -> dict[str, Any]:
    return {
        "system": _ENTERPRISE_SYSTEM_PROMPT,
        "prologue": "你好，我是设备知识库助手。",
        "parameters": [
            {"key": "knowledge", "optional": False},
            {"key": "date", "optional": True},
        ],
        "empty_response": ABSTAIN_PHRASE,
        "quote": True,
        "tts": False,
        "refine_multiturn": True,
        "reference_metadata": {
            "include": True,
            "fields": list(REFERENCE_METADATA_FIELDS),
        },
    }


INVENTORY_QUESTION_RULE = (
    "用户问「有哪些信息/资料/文档/文件/内容」或「现有哪些资料」时，"
    "必须概括本轮真实命中的文档类型（如发票、收据、合格证、调试记录），"
    "可以引用并标 [ID:n]；现有单据类型本身就是答案。"
    f"禁止因此写约定拒答句「{ABSTAIN_PHRASE}」。"
)
_OPERATOR_INVENTORY_LINES = (
    "用户问「有哪些资料/文档」且概括真实命中内容时，可以引用并标 `[ID:n]`。",
    "用户问「有哪些资料/文档」且概括真实命中内容时，可以引用并标 [ID:n]。",
)


def apply_inventory_rule_to_operator_prompt(system: str) -> str:
    """Insert the inventory-question rule without replacing operator prompts."""
    text = system or ""
    if "现有单据类型本身就是答案" in text:
        return text
    for old in _OPERATOR_INVENTORY_LINES:
        if old in text:
            return text.replace(old, INVENTORY_QUESTION_RULE, 1)
    return text


def needs_enterprise_prompt_upgrade(chat: dict | None) -> bool:
    """True when chat is missing the current enterprise prompt marker or metadata."""
    prompt_config = (chat or {}).get("prompt_config") or {}
    system = prompt_config.get("system") or ""
    if ENTERPRISE_PROMPT_MARKER not in system:
        return True
    parameters = prompt_config.get("parameters") or []
    if not any(isinstance(p, dict) and p.get("key") == "knowledge" for p in parameters):
        return True
    ref = prompt_config.get("reference_metadata") or {}
    if not ref.get("include"):
        return True
    fields = ref.get("fields") or []
    return list(fields) != list(REFERENCE_METADATA_FIELDS)


def enterprise_prompt_config_for_api() -> dict[str, Any]:
    """Return a fresh prompt_config payload for create/update chat."""
    return deepcopy(build_enterprise_prompt_config())

