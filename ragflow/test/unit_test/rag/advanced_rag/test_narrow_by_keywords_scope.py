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

"""Lightweight --noconftest tests for scoped keyword narrow."""

import importlib.util
import sys
import types
from pathlib import Path


def _load_search_module():
    """Load harness search.py without package __init__ / settings / navigation."""
    path = (
        Path(__file__).resolve().parents[4]
        / "rag"
        / "advanced_rag"
        / "harness"
        / "tools"
        / "search.py"
    )
    name = "harness_search_under_test"
    if name in sys.modules:
        return sys.modules[name]

    if "common" not in sys.modules:
        common = types.ModuleType("common")
        settings = types.ModuleType("common.settings")
        settings.retriever = None
        common.settings = settings
        sys.modules["common"] = common
        sys.modules["common.settings"] = settings

    nav_name = "rag.advanced_rag.harness.tools.navigation"
    if nav_name not in sys.modules:
        nav = types.ModuleType(nav_name)
        nav._kg_scopes = lambda *a, **k: None
        # Relative import in search.py is `from .navigation import _kg_scopes`
        # so also stub the package path used by importlib relative resolution.
        pkg_tools = "rag.advanced_rag.harness.tools"
        if "rag" not in sys.modules:
            sys.modules["rag"] = types.ModuleType("rag")
        if "rag.advanced_rag" not in sys.modules:
            sys.modules["rag.advanced_rag"] = types.ModuleType("rag.advanced_rag")
        if "rag.advanced_rag.harness" not in sys.modules:
            sys.modules["rag.advanced_rag.harness"] = types.ModuleType("rag.advanced_rag.harness")
        if pkg_tools not in sys.modules:
            tools_pkg = types.ModuleType(pkg_tools)
            tools_pkg.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg_tools] = tools_pkg
        sys.modules[nav_name] = nav
        sys.modules[pkg_tools].navigation = nav  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "rag.advanced_rag.harness.tools"
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _chunk(content: str, cid: str = "c1") -> dict:
    return {"id": cid, "content": content, "content_with_weight": content}


def test_ignore_device_code_keeps_content_keyword_hit():
    """ignore DeviceCode + content keyword → keep & narrow without body DeviceCode."""
    mod = _load_search_module()
    chunks = [_chunk("产品型号为 XT30D。本页为合格证扫描件。")]
    out = mod._narrow_by_keywords(
        chunks,
        "GQ01250024,合格证,产品型号",
        ignore_tokens=["GQ01250024"],
    )
    assert len(out) == 1
    text = out[0]["content_with_weight"]
    assert "合格证" in text or "XT30D" in text or "产品型号" in text
    assert "GQ01250024" not in (chunks[0].get("content") or "")


def test_only_device_code_keyword_ignored_returns_original():
    """Only DeviceCode as keywords + ignore hit → no filter, original chunks."""
    mod = _load_search_module()
    original = "正文无台账号，仅有其它说明。"
    chunks = [_chunk(original)]
    out = mod._narrow_by_keywords(
        chunks,
        "GQ01250024",
        ignore_tokens=["GQ01250024"],
    )
    assert len(out) == 1
    assert out[0]["content_with_weight"] == original


def test_keep_unmatched_when_scoped_preserves_chunks():
    """Scoped + content keywords miss → return original chunks."""
    mod = _load_search_module()
    original = "本段完全无关内容，不含目标词。"
    chunks = [_chunk(original)]
    out = mod._narrow_by_keywords(
        chunks,
        "合格证,产品型号,出厂编号",
        keep_unmatched_when_scoped=True,
    )
    assert len(out) == 1
    assert out[0]["content_with_weight"] == original


def test_unscoped_unmatched_still_drops():
    """Open-domain: unmatched keywords still yield empty list."""
    mod = _load_search_module()
    chunks = [_chunk("本段完全无关内容，不含目标词。")]
    out = mod._narrow_by_keywords(
        chunks,
        "合格证,产品型号,出厂编号",
        keep_unmatched_when_scoped=False,
    )
    assert out == []
