"""Split RAGFlow think tags from the user-facing answer. No heuristic cuts."""

from enterprise.gateway.query.answer_split import (
    StreamThinkSplitter,
    finalize_streamed_output,
    split_assistant_output,
)


def test_paired_think_tags_go_to_reasoning():
    result = split_assistant_output("<think>规划过程</think>你好呀😊！")

    assert result.answer == "你好呀😊！"
    assert result.reasoning == "规划过程"


def test_closing_think_only_splits_on_the_last_marker():
    result = split_assistant_output("规划过程</think>\n你好呀")

    assert result.answer == "你好呀"
    assert result.reasoning == "规划过程"


def test_untagged_planning_stays_in_answer():
    raw = (
        "用户再次打招呼，按照之前的回复风格，继续友好回应。"
        "\n\n你好呀😊！如果你需要查询设备台账信息，都可以说明。"
    )
    result = split_assistant_output(raw)

    assert result.answer == raw
    assert result.reasoning == ""


def test_multiple_think_blocks_are_joined():
    result = split_assistant_output(
        "<think>先核对范围</think>正文甲<think>再组织措辞</think>正文乙"
    )

    assert result.answer == "正文甲正文乙"
    assert result.reasoning == "先核对范围\n再组织措辞"


def test_think_only_leaves_empty_answer():
    result = split_assistant_output("<think>只有思考</think>")

    assert result.answer == ""
    assert result.reasoning == "只有思考"


def test_empty_input_is_empty_answer():
    result = split_assistant_output("")

    assert result.answer == ""
    assert result.reasoning == ""


def test_stream_flags_route_deltas_to_reasoning_then_answer():
    splitter = StreamThinkSplitter()

    assert splitter.feed("", start_to_think=True) == []
    assert splitter.feed("规划") == [("reasoning", "规划")]
    assert splitter.feed("", end_to_think=True) == []
    assert splitter.feed("你好呀") == [("answer", "你好呀")]


def test_stream_embedded_tags_split_the_same_delta():
    splitter = StreamThinkSplitter()

    assert splitter.feed("<think>规划</think>你好呀") == [
        ("reasoning", "规划"),
        ("answer", "你好呀"),
    ]


_TIMELINE_HTML = (
    "<p><em>Structured safe execution timeline (no prompt / knowledge / tool bodies).</em></p>\n"
    '<details class="think-stage"><summary><strong>warmup</strong></summary>\n'
    "<ul>\n<li><code>status</code>: success</li>\n</ul>\n"
    "</details>"
)


def test_think_stage_html_inside_think_tags_goes_to_reasoning():
    result = split_assistant_output(f"<think>{_TIMELINE_HTML}</think>FINAL_ANSWER")

    assert result.answer == "FINAL_ANSWER"
    assert "think-stage" in result.reasoning
    assert "Structured safe execution timeline" in result.reasoning
    assert "think-stage" not in result.answer


def test_think_stage_html_without_think_tags_is_stripped_from_answer():
    result = split_assistant_output(f"{_TIMELINE_HTML}\nFINAL_ANSWER")

    assert result.answer == "FINAL_ANSWER"
    assert "think-stage" in result.reasoning
    assert "warmup" in result.reasoning
    assert "Structured safe execution timeline" not in result.answer


def test_damaged_empty_think_wrappers_around_timeline_are_repaired():
    # Verified leak sample shape: "<>" prefix after <think> corruption.
    raw = f"<>{_TIMELINE_HTML}</>FINAL_ANSWER"
    result = split_assistant_output(raw)

    assert result.answer == "FINAL_ANSWER"
    assert "think-stage" in result.reasoning
    assert "<>" not in result.answer
    assert "think-stage" not in result.answer


def test_finalize_streamed_output_strips_leaked_timeline_from_accumulated_answer():
    leaked = f"<>{_TIMELINE_HTML}\nFINAL_ANSWER"
    result = finalize_streamed_output(leaked, "", None)

    assert result.answer == "FINAL_ANSWER"
    assert "think-stage" in result.reasoning


def test_finalize_prefers_final_delta_with_timeline_when_buffers_empty():
    final = f"<think>{_TIMELINE_HTML}</think>FINAL_ANSWER"
    result = finalize_streamed_output("", "", final)

    assert result.answer == "FINAL_ANSWER"
    assert "think-stage" in result.reasoning


def test_finalize_keeps_clean_streamed_answer_without_timeline():
    result = finalize_streamed_output("CLEAN_ANSWER", "planning notes", None)

    assert result.answer == "CLEAN_ANSWER"
    assert result.reasoning == "planning notes"
