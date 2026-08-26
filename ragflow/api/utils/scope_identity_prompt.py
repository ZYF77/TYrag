#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Scope-identity knowledge preamble for retrieval-scoped equipment Q&A.

Shared by simple ``async_chat`` and Agentic ``formalize_answer`` so both paths
inject the same Chinese identity block into Evidence / knowledges.
"""

# Gateway scope / equipment tokens are short alphanumeric ids (typically 1–2).
_SCOPE_IDENTITY_MAX_IDS = 4
_SCOPE_IDENTITY_MAX_TOKEN_LEN = 64


def _is_device_like_scope_token(token: str) -> bool:
    """True for equipment/fixed-asset style tokens; rejects full questions."""
    text = str(token or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if len(text) > _SCOPE_IDENTITY_MAX_TOKEN_LEN:
        return False
    has_alpha = any(c.isalpha() for c in text)
    has_digit = any(c.isdigit() for c in text)
    has_sep = any(c in "-_." for c in text)
    return has_alpha and (has_digit or has_sep)


def _filter_scope_device_identifiers(candidates, *, reject_values=()) -> list[str]:
    """Dedupe device-like identifiers; never keep a rejected full-question string."""
    reject = {str(v).strip() for v in reject_values if v and str(v).strip()}
    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates or ():
        token = str(raw or "").strip()
        if not token or token in seen or token in reject:
            continue
        if not _is_device_like_scope_token(token):
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= _SCOPE_IDENTITY_MAX_IDS:
            break
    return out


def _build_scope_identity_knowledge_block(identifiers, *, reject_values=()) -> str | None:
    """Fixed Chinese preamble: this turn's retrieval scope maps to these equipment IDs."""
    ids = _filter_scope_device_identifiers(identifiers, reject_values=reject_values)
    if not ids:
        return None
    joined = "、".join(ids)
    return (
        "【本轮检索范围设备身份】\n"
        f"本轮检索范围对应的设备标识为：{joined}。\n"
        "下列资料即属于上述设备（以 document_metadata.equipment_id / fixed_asset_no 为准）。\n"
        "禁止写「无法按该编号匹配」或「正文未找到该设备号」；"
        "应直接依据 Content 回答型号、出厂编号等事实。"
    )


def _prepend_scope_identity_knowledge(knowledges, identifiers, *, reject_values=()):
    block = _build_scope_identity_knowledge_block(identifiers, reject_values=reject_values)
    if not block:
        return knowledges
    return [block, *(knowledges or [])]
