"""Tests for structured safe Thinking timeline (scheme A lightbulb)."""

from rag.advanced_rag.think_timeline import (
    begin_think_timeline,
    display_stage_name,
    end_think_timeline,
    record_think_timeline_from_agentic_log,
    record_think_timeline_stage,
    render_think_timeline,
    reset_think_timeline,
    snapshot_think_timeline,
    stages_from_flat_lines,
)
from rag.advanced_rag.think_log import public_think_log_detail
from rag import diagnostics


SECRET = "SECRET_PROMPT_BODY_SHOULD_NEVER_LEAK"
SECRET_Q = '用户问题："设备密码是 123456\""'


def test_display_stage_names_align_with_tracing():
    assert display_stage_name("model_bind") == "warmup"
    assert display_stage_name("metadata_filter") == "scope"
    assert display_stage_name("refine_multiturn") == "understand"
    assert display_stage_name("retrieval") == "retrieval"
    assert display_stage_name("rerank") == "rerank"
    assert display_stage_name("answer_generation") == "llm"
    assert display_stage_name("reference_metadata") == "citation"
    assert display_stage_name("[Hybrid search]") == "Hybrid search"
    assert display_stage_name("[Planner]") == "Planner"


def test_timeline_records_safe_meta_only_and_renders_nested_details():
    token = begin_think_timeline()
    try:
        record_think_timeline_stage(
            "retrieval",
            meta={
                "durationMs": 12.5,
                "status": "success",
                "hitCount": 3,
                "prompt": SECRET,
                "content": SECRET,
                "toolResult": SECRET,
            },
        )
        record_think_timeline_from_agentic_log("[Hybrid search] ran vector and keyword retrieval")
        rendered = render_think_timeline()
        entries = snapshot_think_timeline()
    finally:
        reset_think_timeline(token)

    assert len(entries) == 2
    assert entries[0]["display"] == "retrieval"
    assert entries[0]["meta"]["hitCount"] == 3
    assert "prompt" not in entries[0]["meta"]
    assert SECRET not in rendered
    assert 'class="think-stage"' in rendered
    assert "<details" in rendered
    assert "retrieval" in rendered
    assert "Hybrid search" in rendered


def test_public_think_log_detail_feeds_timeline_without_raw_body():
    token = begin_think_timeline()
    try:
        detail = public_think_log_detail(f'[Planner] Working out how to research: "{SECRET_Q}"')
        entries = snapshot_think_timeline()
    finally:
        reset_think_timeline(token)

    assert detail == "[Planner] planned the retrieval steps"
    assert SECRET_Q not in detail
    assert SECRET not in detail
    assert len(entries) == 1
    assert entries[0]["display"] == "Planner"
    assert SECRET not in render_think_timeline(entries)


def test_record_timed_rag_stage_mirrors_into_active_timeline():
    diag = diagnostics.begin_rag_diagnostics(True, "run-timeline")
    token = begin_think_timeline()
    try:
        diagnostics.record_timed_rag_stage(
            "answer_generation",
            __import__("time").perf_counter(),
            mode="stream",
            status="success",
            prompt=SECRET,
        )
        entries = snapshot_think_timeline()
        snap = diagnostics.snapshot_rag_diagnostics()
    finally:
        reset_think_timeline(token)
        diagnostics.reset_rag_diagnostics(diag)

    assert entries[0]["display"] == "llm"
    assert entries[0]["meta"]["mode"] == "stream"
    assert "prompt" not in entries[0]["meta"]
    assert SECRET not in render_think_timeline(entries)
    assert SECRET not in str(snap)


def test_stages_from_flat_lines_roundtrip():
    entries = stages_from_flat_lines(
        ["[Hybrid search] ran vector and keyword retrieval", "[Composing the answer] composed the answer from evidence"]
    )
    rendered = render_think_timeline(entries)
    assert "Hybrid search" in rendered
    assert "Composing the answer" in rendered
    assert 'class="think-stage"' in rendered


def test_end_think_timeline_deactivates():
    begin_think_timeline()
    record_think_timeline_stage("warmup", meta={"status": "success"})
    end_think_timeline()
    assert snapshot_think_timeline() == []
