"""Malformed inline citation markers must be repaired or removed before EAM sees them."""

from enterprise.gateway.query.citation_select import sanitize_citation_markers


def test_keeps_canonical_id_markers():
    text = "手册说明[ID:2]，工单已处理[ID:1]。"
    assert sanitize_citation_markers(text) == text


def test_repairs_double_bracket_and_nested_id_forms():
    text = (
        "数据存储停止 [ID:2][ID[3]][[ID:4]][[ID:5]]。"
        "画面 [I[D]:2] 与 [[I:D]:3] 以及 [[D]:1]。"
    )
    cleaned = sanitize_citation_markers(text)
    assert cleaned == (
        "数据存储停止 [ID:2][ID:3][ID:4][ID:5]。"
        "画面 [ID:2] 与 [ID:3] 以及 [ID:1]。"
    )


def test_strips_time_fragment_brackets_and_empty_markers():
    text = (
        "时间点包括 2024/11/29 15:02:[1] [ID:0][[D]:1] "
        "以及 2024/12/2 的 12:[7]:5、12:[6]:4[ ]等时刻 "
        "[I[D]:2][][[I:D]:3][][[I:D]:4][][[I:D]:5]]。"
    )
    cleaned = sanitize_citation_markers(text)
    assert "15:02:1" in cleaned or "15:02" in cleaned
    assert "[1]" not in cleaned
    assert "12:7:5" in cleaned
    assert "12:6:4" in cleaned
    assert "[]" not in cleaned
    assert "[ID:0]" in cleaned
    assert "[ID:1]" in cleaned
    assert "[ID:2]" in cleaned
    assert "[ID:3]" in cleaned
    assert "[ID:4]" in cleaned
    assert "[ID:5]" in cleaned
    assert "[I[D]" not in cleaned
    assert "[[I:D]" not in cleaned
    assert "[[D]" not in cleaned


def test_canonicalizes_bare_digit_markers_to_id_form():
    assert sanitize_citation_markers("见文档[2]与[0]。") == "见文档[ID:2]与[ID:0]。"


def test_drops_unrecoverable_citation_garbage():
    cleaned = sanitize_citation_markers("说明[ID:[[[[ 以及正常[ID:0]。")
    assert cleaned == "说明 以及正常[ID:0]。"
    assert "[[[[" not in cleaned
