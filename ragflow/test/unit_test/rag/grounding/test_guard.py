"""Pure lexical checks for the RAGFlow identifier/numeric fuse."""

from types import SimpleNamespace

from rag.grounding.guard import (
    STANDARD_ABSTAIN_ANSWER,
    apply_identifier_numeric_fuse,
    evaluate_grounding,
    is_grounded,
    visible_answer_text,
)


def test_exact_identifier_and_number_are_grounded():
    assert is_grounded(
        "设备 EQ-104 压力 2 MPa",
        "设备编号 EQ-104，额定压力 2 MPa。",
    )


def test_unknown_identifier_or_number_fails():
    assert not is_grounded("设备 EQ-105", "设备编号 EQ-104")
    assert not is_grounded("压力 3 MPa", "压力 2 MPa")


def test_citation_and_markdown_number_noise_is_ignored():
    assert is_grounded(
        "1. 结论 [ID:1] [2] 知识库ID:3 ID:4\n2) 已核对。",
        "完全不同的正文。",
    )


def test_inline_list_ordinals_are_not_numeric_claims():
    assert is_grounded(
        "资料包括：1. 运行手册；2. 巡检说明。[ID:0]",
        "西门子变频器运行手册。日常巡检说明。",
    )
    # Decimals / versions must still be checked.
    assert not is_grounded("压力 1.5 MPa", "压力 2 MPa")
    assert is_grounded("压力 1.5 MPa", "额定压力 1.5 MPa")


def test_retrieval_meta_counts_are_not_numeric_claims():
    knowledge = "西门子变频器运行手册。日常巡检说明。"
    assert is_grounded("检索到6条相关片段，均为西门子运行资料。[ID:0]", knowledge)
    assert is_grounded("现有资料共2类：运行手册与巡检说明。[ID:0]", knowledge)
    assert is_grounded("共2份资料，包括运行手册与巡检说明。[ID:0]", knowledge)
    # Real quantity facts stay strict.
    assert not is_grounded("共有3条维修记录。[ID:0]", knowledge)
    assert is_grounded("共有3条维修记录。[ID:0]", "设备维修记录共有3条。")


def test_zero_placeholder_measurements_can_be_stripped():
    from rag.grounding.guard import strip_ungrounded_zero_measurements

    knowledge = "西门子变频器运行手册。包含频率、电流、温度等巡检项。"
    raw = "运行信息包括频率 0 Hz、电流 0 A、温度 0 °C，详见运行手册。[ID:0]"
    assert not is_grounded(raw, knowledge)
    cleaned = strip_ungrounded_zero_measurements(raw)
    assert "0 Hz" not in cleaned and "0 A" not in cleaned
    assert is_grounded(cleaned, knowledge)
    # Non-zero invented readings must still fail.
    assert not is_grounded("温度 36 °C，详见运行手册。[ID:0]", knowledge)


def test_nfkc_case_and_whitespace_normalisation():
    assert is_grounded(
        "ＥＱ－１０４\u00a0压力：2\u00a0MPa",
        "eq-104 压力：2000 kPa",
    )


def test_identifier_digits_do_not_ground_standalone_numbers():
    assert not is_grounded("编号 123", "设备 EQ123")


def test_only_kpa_mpa_conversion_is_supported():
    assert is_grounded("压力 2 MPa", "压力 2000 kPa")
    assert not is_grounded("压力 2 bar", "压力 2000 kPa")


def test_fraction_is_not_a_percent():
    assert not is_grounded("完成率 20%", "完成率 1/5")
    assert is_grounded("完成率 1/5", "完成率 1/5")


def test_attachment_value_is_allowed_only_as_an_observation():
    observation = [
        SimpleNamespace(
            text_spans=[],
            error_codes=[],
            equipment_codes=[],
            visible_values=["2 MPa"],
        )
    ]
    assert is_grounded("附件显示压力为 2 MPa", "无数值知识", observation)
    assert not is_grounded("设备当前压力为 2 MPa", "无数值知识", observation)
    assert not is_grounded(
        "附件显示设备参数要求为 2 MPa", "无数值知识", observation
    )


def test_attachment_identifier_can_be_reported_as_suspected_observation():
    observation = [
        SimpleNamespace(
            text_spans=[],
            error_codes=["E-104"],
            equipment_codes=[],
            visible_values=[],
        )
    ]
    assert is_grounded("附件中识别到疑似故障码 E-104", "", observation)
    assert is_grounded("当前附件显示故障码 E-104", "", observation)
    assert not is_grounded("识别到故障码 E-104", "", observation)
    assert not is_grounded("设备当前故障码为 E-104", "", observation)


def test_attachment_can_report_a_visible_equipment_code():
    observation = [
        SimpleNamespace(
            text_spans=[],
            error_codes=[],
            equipment_codes=["GD01250002"],
            visible_values=[],
        )
    ]
    assert is_grounded("附件显示设备号 GD01250002", "", observation)


def test_chinese_number_with_unit_requires_exact_text():
    assert is_grounded("共三台", "清单共三台")
    assert not is_grounded("共四台", "清单共三台")


def test_bound_equipment_id_is_allowed_without_knowledge_text():
    assert is_grounded(
        "设备 EQ-BOUND-001 发票齐全。",
        "发票和收据各一份，未见其它参数。",
        allowed_identifiers=["EQ-BOUND-001"],
    )


def test_bound_fixed_asset_no_is_allowed_without_knowledge_text():
    assert is_grounded(
        "固定资产 FA-BOUND-001 属于本台设备。",
        "发票和收据各一份，未见其它参数。",
        allowed_identifiers=["FA-BOUND-001"],
    )


def test_question_identifier_is_allowed_without_knowledge_text():
    assert is_grounded(
        "GI01240015 有发票和收据。",
        "发票和收据各一份。",
        allowed_identifiers=["GI01240015"],
    )


def test_unrelated_equipment_id_still_fails_with_allowed_identifiers():
    assert not is_grounded(
        "设备 EQ-OTHER-999 运行正常。",
        "发票和收据各一份。",
        allowed_identifiers=["EQ-BOUND-001"],
    )


def test_numbers_still_require_knowledge_even_with_allowed_identifiers():
    assert not is_grounded(
        "设备 EQ-BOUND-001 压力 3 MPa",
        "发票和收据各一份。",
        allowed_identifiers=["EQ-BOUND-001"],
    )
    assert is_grounded(
        "设备 EQ-BOUND-001 压力 2 MPa",
        "额定压力 2 MPa。",
        allowed_identifiers=["EQ-BOUND-001"],
    )


def test_attachment_observation_rules_unchanged_with_allowed_identifiers():
    observation = [
        SimpleNamespace(
            text_spans=[],
            error_codes=[],
            equipment_codes=[],
            visible_values=["2 MPa"],
        )
    ]
    assert is_grounded(
        "附件显示压力为 2 MPa",
        "无数值知识",
        observation,
        allowed_identifiers=["EQ-BOUND-001"],
    )
    assert not is_grounded(
        "设备当前压力为 2 MPa",
        "无数值知识",
        observation,
        allowed_identifiers=["EQ-BOUND-001"],
    )
    assert not is_grounded(
        "附件显示设备参数要求为 2 MPa",
        "无数值知识",
        observation,
        allowed_identifiers=["EQ-BOUND-001"],
    )


def test_evaluate_grounding_reports_unmatched_counts():
    result = evaluate_grounding(
        "设备 EQ-OTHER-999 压力 3 MPa",
        "发票和收据各一份。",
        allowed_identifiers=["EQ-BOUND-001"],
    )
    assert result.passed is False
    assert result.unmatched_identifiers == 1
    assert result.unmatched_numbers == 1

    allowed = evaluate_grounding(
        "设备 EQ-BOUND-001 发票齐全。",
        "发票和收据各一份。",
        allowed_identifiers=["EQ-BOUND-001"],
    )
    assert allowed.passed is True
    assert allowed.unmatched_identifiers == 0
    assert allowed.unmatched_numbers == 0


def test_fuse_ignores_think_block_and_self_computed_percent_fails():
    result = apply_identifier_numeric_fuse(
        "<think>WO-99999 20%</think>压力 2 MPa",
        "压力 2000 kPa",
    )
    assert result.passed is True
    assert visible_answer_text("<think>hidden WO-99999</think>ok") == "ok"

    computed = apply_identifier_numeric_fuse("完成率 20%", "完成率 1/5")
    assert computed.passed is False
    assert STANDARD_ABSTAIN_ANSWER == "未找到可靠依据，无法回答。"
