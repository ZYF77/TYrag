"""Enterprise Chat prompt for equipment identity vs content relevance.

Identity lives in document meta_fields and is exposed as document_metadata.
Content facts must still come from chunk Content. This module only defines
the generation-side rules; it does not implement Grounding Guard / Web Search.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

ENTERPRISE_PROMPT_MARKER = "enterprise_identity_metadata_v1"

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
如果 Content 无法支持该问题，应明确说明
当前检索结果中没有找到可靠依据，不得根据设备归属推测。

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
