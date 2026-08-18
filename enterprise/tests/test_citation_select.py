"""Citation chunks must match answer [ID:n] markers, and stay empty when abstaining."""

from enterprise.gateway.query.citation_select import (
    ABSTAIN_PHRASE,
    force_abstain_outcome,
    select_cited_chunks,
)


CHUNKS = [
    {"id": "invoice", "content": "Cursor Pro invoice"},
    {"id": "repair", "content": "leak repair work order"},
    {"id": "manual", "content": "maintenance manual"},
]


def test_keeps_only_chunks_marked_in_the_answer():
    selected = select_cited_chunks(
        "根据工单，漏气已处理。[ID:1]",
        CHUNKS,
        status="completed",
    )

    assert [item["id"] for item in selected] == ["repair"]


def test_keeps_multiple_marked_chunks_in_first_seen_order():
    selected = select_cited_chunks(
        "手册 [ID:2] 与工单 [ID:1] [ID:2]",
        CHUNKS,
        status="completed",
    )

    assert [item["id"] for item in selected] == ["manual", "repair"]


def test_ignores_out_of_range_and_unmarked_chunks():
    selected = select_cited_chunks(
        "没有标引用，或标了不存在的 [ID:9]",
        CHUNKS,
        status="completed",
    )

    assert selected == []


def test_keeps_prose_knowledge_id_citations():
    selected = select_cited_chunks(
        "存在开箱验收移交单（知识库ID:0、ID:2），但未写验收人。以ID:2的文档为例。",
        CHUNKS,
        status="completed",
    )

    assert [item["id"] for item in selected] == ["invoice", "manual"]


def test_oob_prose_ids_keep_overlapping_current_turn_chunks():
    selected = select_cited_chunks(
        "当前检索到的设备开箱验收移交单（知识库ID:5）中，未记录验收人。",
        [{"id": "unpack", "content": "浙江天台药业 设备开箱验收移交单 验收移交人"}],
        status="completed",
    )

    assert [item["id"] for item in selected] == ["unpack"]


def test_oob_prose_ids_skip_non_overlapping_retrieval_hits():
    selected = select_cited_chunks(
        "以下是西门子 SIMATICHMI 运行画面文本信息（知识库ID:2、ID:5）。",
        [
            {
                "id": "ops",
                "content": "进行接线、维修时请对断路器上锁挂牌。全开式耙式真空干燥机操作说明",
            }
        ],
        status="completed",
    )

    assert selected == []


def test_oob_prose_ids_do_not_keep_large_unmatched_sets():
    many = [{"id": f"c{i}", "content": f"无关文档片段编号{i}一二三四五六"} for i in range(6)]
    selected = select_cited_chunks(
        "根据知识库ID:9说明西门子运行画面如下。",
        many,
        status="completed",
    )

    assert selected == []


def test_no_reliable_evidence_clears_citations_even_when_answer_has_markers():
    selected = select_cited_chunks(
        "当前检索结果中没有找到可靠依据。[ID:0][ID:1]",
        CHUNKS,
        status="no_reliable_evidence",
    )

    assert selected == []


def test_no_reliable_evidence_clears_prose_id_citations():
    selected = select_cited_chunks(
        "暂无专门的设备维修记录。知识库ID:0、ID:1。",
        CHUNKS,
        status="no_reliable_evidence",
    )

    assert selected == []


def test_force_abstain_outcome_overrides_completed_when_phrase_present():
    answer = f"暂无维修记录。{ABSTAIN_PHRASE}。[ID:0][ID:1]"
    status = force_abstain_outcome(answer, "completed")

    assert status == "no_reliable_evidence"
    assert select_cited_chunks(answer, CHUNKS, status) == []


def test_force_abstain_outcome_catches_paraphrased_no_repair_answer():
    answer = (
        "当前检索到的知识库中，仅包含调试记录[ID:4]和合格证[ID:0]，"
        "暂无专门的设备维修记录。"
    )
    status = force_abstain_outcome(answer, "completed")

    assert status == "no_reliable_evidence"
    assert select_cited_chunks(answer, CHUNKS, status) == []


def test_force_abstain_outcome_leaves_completed_without_phrase():
    answer = "漏气维修见工单。[ID:1]"
    status = force_abstain_outcome(answer, "completed")

    assert status == "completed"
    assert [item["id"] for item in select_cited_chunks(answer, CHUNKS, status)] == [
        "repair"
    ]


def test_force_abstain_outcome_leaves_inventory_answer():
    answer = "该设备资料包括合格证[ID:0]与调试记录[ID:1]。"
    status = force_abstain_outcome(answer, "completed")

    assert status == "completed"
    assert [item["id"] for item in select_cited_chunks(answer, CHUNKS, status)] == [
        "invoice",
        "repair",
    ]


def test_force_abstain_outcome_does_not_override_failed():
    assert (
        force_abstain_outcome(f"{ABSTAIN_PHRASE} [ID:0]", "failed") == "failed"
    )
