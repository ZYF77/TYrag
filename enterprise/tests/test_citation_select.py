"""Citation chunks match answer markers independently from business status."""

from enterprise.gateway.query.citation_select import (
    ABSTAIN_PHRASE,
    select_cited_chunk_refs,
    select_cited_chunks,
)
from enterprise.gateway.query.workflow_router import _workflow_chunks


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


def test_select_cited_chunk_refs_preserves_marker_indexes():
    selected = select_cited_chunk_refs(
        "手册 [ID:2] 与工单 [ID:1] [ID:2]",
        CHUNKS,
        status="completed",
    )

    assert [(item["id"], ref) for item, ref in selected] == [
        ("manual", 2),
        ("repair", 1),
    ]


def test_overlap_fallback_refs_have_null_ref_index():
    answer = "leak repair work order already handled for this asset."
    selected = select_cited_chunk_refs(
        f"{answer} [ID:99]",
        CHUNKS,
        status="completed",
    )

    assert len(selected) == 1
    assert selected[0][0]["id"] == "repair"
    assert selected[0][1] is None


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


def test_no_reliable_evidence_keeps_citations_when_answer_has_markers():
    selected = select_cited_chunks(
        "当前检索结果中没有找到可靠依据。[ID:0][ID:1]",
        CHUNKS,
        status="no_reliable_evidence",
    )

    assert [item["id"] for item in selected] == ["invoice", "repair"]


def test_no_reliable_evidence_keeps_prose_id_citations():
    selected = select_cited_chunks(
        "暂无专门的设备维修记录。知识库ID:0、ID:1。",
        CHUNKS,
        status="no_reliable_evidence",
    )

    assert [item["id"] for item in selected] == ["invoice", "repair"]


# --- REPORT discrete citation_id regressions (Workflow vs Chat) ---

DISCRETE_CHUNKS = [
    {
        "id": "459",
        "citation_id": "459",
        "content": "root cause valve packing leak on pump A",
    },
    {
        "id": "66",
        "citation_id": "66",
        "content": "maintenance history for gearbox oil seal",
    },
]


def test_workflow_chunks_preserves_discrete_citation_ids_from_map_keys():
    chunks = _workflow_chunks(
        {
            "459": {"doc_id": "d1", "content": "root cause valve packing leak on pump A"},
            "66": {"doc_id": "d2", "content": "maintenance history for gearbox oil seal"},
        }
    )

    assert [c["citation_id"] for c in chunks] == ["459", "66"]
    assert [c["id"] for c in chunks] == ["459", "66"]


def test_discrete_citation_ids_match_exact_markers_not_list_order():
    selected = select_cited_chunk_refs(
        "依据 [ID:66] 与 [ID:459]",
        DISCRETE_CHUNKS,
        status="completed",
    )

    assert [(item["id"], ref) for item, ref in selected] == [
        ("66", 66),
        ("459", 459),
    ]


def test_discrete_citation_id_does_not_fall_back_to_list_position():
    # List position 0 is chunk 459; citing [ID:0] must not bind that chunk.
    selected = select_cited_chunk_refs(
        "错误序号 [ID:0] 不应命中；正确是 [ID:66]",
        DISCRETE_CHUNKS,
        status="completed",
    )

    assert [(item["id"], ref) for item, ref in selected] == [("66", 66)]


def test_unknown_discrete_citation_id_returns_empty_without_index_fallback():
    selected = select_cited_chunk_refs(
        "未知引用 [ID:1]",
        DISCRETE_CHUNKS,
        status="completed",
    )

    assert selected == []


def test_unknown_discrete_id_skips_overlap_fallback_even_without_abstain():
    # Explicit citation_id mode must not silently overlap-fallback to list[1].
    chunks = [
        {"id": "459", "citation_id": "459", "content": "unrelated alpha text"},
        {
            "id": "66",
            "citation_id": "66",
            "content": "leak repair work order already handled for this asset",
        },
    ]
    answer = "leak repair work order already handled for this asset. [ID:1]"
    selected = select_cited_chunk_refs(answer, chunks, status="completed")

    assert selected == []


def test_discrete_cites_with_partial_abstain_keep_exact_matches_only():
    selected = select_cited_chunk_refs(
        f"{ABSTAIN_PHRASE} 但可见片段 [ID:66]。未知 [ID:1]。",
        DISCRETE_CHUNKS,
        status="no_reliable_evidence",
    )

    assert [(item["id"], ref) for item, ref in selected] == [("66", 66)]


def test_chat_path_without_citation_id_keeps_sequential_indexes():
    # Ordinary Chat chunks lack citation_id; preserve list-index binding.
    selected = select_cited_chunk_refs(
        "手册 [ID:2] 与工单 [ID:1]",
        CHUNKS,
        status="completed",
    )

    assert [(item["id"], ref) for item, ref in selected] == [
        ("manual", 2),
        ("repair", 1),
    ]


def test_chat_path_without_citation_id_still_allows_overlap_fallback():
    answer = "leak repair work order already handled for this asset."
    selected = select_cited_chunk_refs(
        f"{answer} [ID:99]",
        CHUNKS,
        status="completed",
    )

    assert len(selected) == 1
    assert selected[0][0]["id"] == "repair"
    assert selected[0][1] is None
