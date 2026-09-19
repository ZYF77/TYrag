"""Unit tests for EAM user long-term Memory helpers (phase 1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from enterprise.gateway.query.memory_client import RAGFlowMemoryStub
from enterprise.gateway.query.memory_subject import (
    is_valid_memory_subject,
    memory_subject_from_principal,
    parse_memory_subject,
)
from enterprise.gateway.query import user_memory as um
from enterprise.gateway.query.enterprise_prompt import (
    build_enterprise_prompt_config,
    needs_enterprise_prompt_upgrade,
)


def test_memory_subject_from_principal():
    principal = SimpleNamespace(tenant_id="t1", business_user_id="u1")
    assert memory_subject_from_principal(principal) == "eam:t1:u1"
    assert is_valid_memory_subject("eam:t1:u1")
    assert parse_memory_subject("eam:t1:u1") == ("t1", "u1")
    with pytest.raises(ValueError):
        memory_subject_from_principal(SimpleNamespace(tenant_id="", business_user_id="u1"))


def test_format_memory_hits():
    text = um.format_memory_hits(
        [
            {"content": "prefers concise answers"},
            {"user_input": "hi", "agent_response": "hello"},
        ],
        top_n=5,
    )
    assert "1. prefers concise answers" in text
    assert "2. User Input: hi" in text


@pytest.mark.asyncio
async def test_fetch_empty_when_disabled(monkeypatch):
    from enterprise.gateway.config import config as gw_config
    gw_config.clear_runtime_settings()
    monkeypatch.setattr(um.config, "user_memory_enabled", False, raising=False)
    monkeypatch.setattr(um.config, "enterprise_memory_id", "mem-1", raising=False)
    text = await um.fetch_user_memory_text(
        SimpleNamespace(tenant_id="t1", business_user_id="u1"),
        "question",
    )
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_and_schedule_with_stub(monkeypatch):
    from enterprise.gateway.config import config as gw_config
    gw_config.clear_runtime_settings()
    stub = RAGFlowMemoryStub()
    await stub.add_message(
        memory_id="mem-1",
        agent_id="chat:c1",
        session_id="s1",
        user_id="eam:t1:u1",
        user_input="prior",
        agent_response="memory content about pumps",
    )
    um.set_memory_client_for_tests(stub)
    monkeypatch.setattr(um.config, "user_memory_enabled", True, raising=False)
    monkeypatch.setattr(um.config, "enterprise_memory_id", "mem-1", raising=False)
    monkeypatch.setattr(um.config, "user_memory_top_n", 5, raising=False)

    principal = SimpleNamespace(tenant_id="t1", business_user_id="u1")
    text = await um.fetch_user_memory_text(principal, "pumps")
    assert "memory content about pumps" in text
    # Isolation: other subject sees nothing
    other = await um.fetch_user_memory_text(
        SimpleNamespace(tenant_id="t1", business_user_id="u2"),
        "pumps",
    )
    assert other == ""

    um.schedule_memory_candidate(
        principal,
        chat_id="c1",
        session_id="s2",
        user_input="new q",
        agent_response="new a",
        request_id="rid-1",
    )
    # Allow the fire-and-forget task to run.
    await asyncio.sleep(0.05)
    assert any(call.get("user_id") == "eam:t1:u1" for call in stub.add_calls)
    um.set_memory_client_for_tests(None)


def test_prompt_requires_user_memory_parameter():
    cfg = build_enterprise_prompt_config()
    assert any(p.get("key") == "user_memory" for p in cfg["parameters"])
    assert "{user_memory}" in cfg["system"]
    assert needs_enterprise_prompt_upgrade({"prompt_config": cfg}) is False
    legacy = dict(cfg)
    legacy["parameters"] = [p for p in cfg["parameters"] if p.get("key") != "user_memory"]
    legacy["system"] = cfg["system"].replace("{user_memory}", "")
    assert needs_enterprise_prompt_upgrade({"prompt_config": legacy}) is True


@pytest.mark.asyncio
async def test_fetch_noop_when_enabled_without_memory_id(monkeypatch):
    from enterprise.gateway.config import config as gw_config

    gw_config.clear_runtime_settings()
    monkeypatch.setattr(um.config, "user_memory_enabled", True, raising=False)
    monkeypatch.setattr(um.config, "enterprise_memory_id", "", raising=False)
    # Reset one-shot warning so this path is exercised cleanly.
    um._warned_missing_config = False
    text = await um.fetch_user_memory_text(
        SimpleNamespace(tenant_id="t1", business_user_id="u1"),
        "question",
    )
    assert text == ""
    assert um.memory_config_ready() is False


@pytest.mark.asyncio
async def test_user_memory_hot_toggle_via_runtime_snapshot(monkeypatch):
    """PUT-equivalent apply_runtime_settings flips Search without process restart."""
    from dataclasses import replace

    from enterprise.gateway.config import GatewayRuntimeSettings, config as gw_config
    from enterprise.gateway.query.memory_client import RAGFlowMemoryStub

    stub = RAGFlowMemoryStub()
    await stub.add_message(
        memory_id="mem-hot",
        agent_id="chat:c1",
        session_id="s1",
        user_id="eam:t1:u1",
        user_input="prior",
        agent_response="hot memory hit about valves",
    )
    um.set_memory_client_for_tests(stub)
    um._warned_missing_config = False
    gw_config.clear_runtime_settings()
    base = GatewayRuntimeSettings.from_config(gw_config)
    off = replace(
        base,
        user_memory_enabled=False,
        user_memory_id="mem-hot",
        user_memory_top_n=5,
        user_memory_timeout_seconds=5.0,
    )
    gw_config.apply_runtime_settings(off)
    principal = SimpleNamespace(tenant_id="t1", business_user_id="u1")
    assert await um.fetch_user_memory_text(principal, "valves") == ""

    on = replace(off, user_memory_enabled=True)
    gw_config.apply_runtime_settings(on)
    text = await um.fetch_user_memory_text(principal, "valves")
    assert "hot memory hit about valves" in text

    # enabled=true + empty memoryId => no-op Write/Search
    empty = replace(on, user_memory_id="")
    gw_config.apply_runtime_settings(empty)
    assert await um.fetch_user_memory_text(principal, "valves") == ""
    before = len(stub.add_calls)
    um.schedule_memory_candidate(
        principal,
        chat_id="c1",
        session_id="s2",
        user_input="q",
        agent_response="a",
    )
    await asyncio.sleep(0.05)
    assert len(stub.add_calls) == before

    um.set_memory_client_for_tests(None)
    gw_config.clear_runtime_settings()
