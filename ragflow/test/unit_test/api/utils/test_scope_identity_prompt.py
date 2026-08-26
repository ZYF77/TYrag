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

from api.utils import scope_identity_prompt as sip


def test_filter_scope_device_identifiers_keeps_equipment_tokens_only():
    question = "GQ01250024 的合格证上产品型号是什么？"
    filtered = sip._filter_scope_device_identifiers(
        ["GQ01250024", "FA-001", question, "型号", ""],
        reject_values=(question,),
    )
    assert filtered == ["GQ01250024", "FA-001"]


def test_build_scope_identity_block_absent_when_empty():
    assert sip._build_scope_identity_knowledge_block([]) is None
    assert sip._build_scope_identity_knowledge_block(None) is None
    assert sip._build_scope_identity_knowledge_block([""]) is None


def test_build_scope_identity_block_includes_forbid_phrases():
    block = sip._build_scope_identity_knowledge_block(["GQ01250024"])
    assert block is not None
    assert "GQ01250024" in block
    assert "document_metadata.equipment_id" in block
    assert "无法按该编号匹配" in block
    assert "正文未找到该设备号" in block


def test_prepend_puts_block_first():
    knowledges = ["k1", "k2"]
    out = sip._prepend_scope_identity_knowledge(knowledges, ["GQ01250024"])
    assert out[0].startswith("【本轮检索范围设备身份】")
    assert out[1:] == ["k1", "k2"]
