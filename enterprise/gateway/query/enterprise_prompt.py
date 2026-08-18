"""Enterprise Chat prompt for equipment identity vs content relevance.

Identity lives in document meta_fields and is exposed as document_metadata.
Content facts must still come from chunk Content. This module only defines
the generation-side rules; it does not implement Grounding Guard / Web Search.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE

ENTERPRISE_PROMPT_MARKER = "enterprise_identity_metadata_v6"

REFERENCE_METADATA_FIELDS = (
    "equipment_id",
    "fixed_asset_no",
    "enterprise_document_type",
    "enterprise_external_document_id",
)

_ENTERPRISE_SYSTEM_PROMPT = f"""你是企业设备知识库助手。请基于知识库内容回答用户问题。
[{ENTERPRISE_PROMPT_MARKER}]

当前提供的知识片段已经按会话范围过滤。

文档是否属于当前设备，以 document_metadata 中的 equipment_id / fixed_asset_no 为准。
不要因为正文中没有重复出现设备编号而否定文档归属。

设备归属不等于内容与问题相关。
metadata 只证明「这是这台设备的文档」，
不能证明「正文包含用户当前问题需要的事实」。
回答具体维修、故障、参数、记录、数量等问题时，
必须由 Content 中的实际内容提供依据。
如果 Content 无法支持该问题（例如用户问维修记录但只有合格证/调试记录），
正文必须原样包含约定拒答句「{ABSTAIN_PHRASE}」，不得根据设备归属推测。
此时禁止任何 [ID:n] / [n] 引用，禁止把合格证、调试记录等无关文档当对照证据引用。
允许用文字说明「现有文档类型」，但不得标引用、不得写出 ID:n / 知识库ID:n。
约定拒答句只用于无法支撑用户当前所问事实的场景，不得用于已答出事实的回答。
用户问「有哪些资料/文档」且概括真实命中内容时，可以引用并标 [ID:n]。

正文一旦依据某个知识片段作答（含「有某类记录/单据，但缺少某一字段」的半支撑回答），
必须在正文用方括号引用格式 [ID:n] 标出该片段，禁止只写「知识库ID:n」「ID:n」散文。
引用编号只能使用本轮知识库列表中的编号，禁止沿用上一轮对话里的 ID；
本轮若只有一个片段，必须标 [ID:0]。
例如：存在开箱验收移交单但未写验收人姓名时，仍应引用验收单片段如 [ID:0]，
并说明缺验收人；不要因此整段改用约定拒答句，也不要省略 [ID:n]。

思考过程只写在 <think>...</think> 内。
标签外只写给用户看的最终正文，不要把规划、自我提醒、对知识库条目的逐条核对写进正文。
禁止把「按照之前的回复风格」「需要检查所有知识库」这类过程文字写进正文。

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
        "empty_response": "未找到可靠依据，无法回答。",
        "quote": True,
        "tts": False,
        "refine_multiturn": True,
        "reference_metadata": {
            "include": True,
            "fields": list(REFERENCE_METADATA_FIELDS),
        },
    }


def needs_enterprise_prompt_upgrade(chat: dict | None) -> bool:
    """True when chat is missing the enterprise identity prompt marker."""
    prompt_config = (chat or {}).get("prompt_config") or {}
    system = prompt_config.get("system") or ""
    if ENTERPRISE_PROMPT_MARKER not in system:
        return True
    ref = prompt_config.get("reference_metadata") or {}
    if not ref.get("include"):
        return True
    fields = ref.get("fields") or []
    return list(fields) != list(REFERENCE_METADATA_FIELDS)


def enterprise_prompt_config_for_api() -> dict[str, Any]:
    """Return a fresh prompt_config payload for create/update chat."""
    return deepcopy(build_enterprise_prompt_config())
