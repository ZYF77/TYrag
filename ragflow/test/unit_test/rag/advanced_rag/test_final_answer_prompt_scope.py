"""Agentic FINAL_ANSWER_SYSTEM — S4/B1 subject-attribute binding rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_final_answer_system() -> str:
    path = (
        Path(__file__).resolve().parents[4]
        / "rag"
        / "advanced_rag"
        / "harness"
        / "prompts"
        / "report_prompt.py"
    )
    spec = importlib.util.spec_from_file_location("report_prompt_scope_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FINAL_ANSWER_SYSTEM


def test_final_answer_system_has_model_part_binding_rules():
    prompt = _load_final_answer_system()
    assert "型号/部件与属性绑定" in prompt
    assert "明确建立该主语与属性值之间的关系" in prompt
    assert "禁止把同名字段从其它对象嫁接到该主语" in prompt
    assert "部件型号不得当作整机身份" in prompt
    assert "无法确认候选值属于用户点名的型号或部件" in prompt
    assert "不得附带本范围内其它型号、出厂编号、项号" in prompt
    assert "{cite_rules}" in prompt
