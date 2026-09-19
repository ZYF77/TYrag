"""Unit checks for the Agent Workflow Retrieval document ceiling."""

from agent.tools.retrieval import Retrieval


def test_scope_filter_drops_expanded_chunks_and_aggregates():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [
                {"doc_id": "doc-keep", "content": "allowed"},
                {"doc_id": "doc-drop", "content": "outside"},
                {"document_id": "doc-keep", "content": "also allowed"},
            ],
            "doc_aggs": [
                {"doc_id": "doc-keep"},
                {"doc_id": "doc-drop"},
            ],
        },
        ["doc-keep"],
    )

    assert [item["content"] for item in result["chunks"]] == [
        "allowed",
        "also allowed",
    ]
    assert result["doc_aggs"] == [{"doc_id": "doc-keep"}]

def test_scope_filter_accepts_doc_id_kwd():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [
                {"doc_id_kwd": "doc-keep", "content": "allowed"},
                {"doc_id_kwd": "doc-drop", "content": "outside"},
            ],
            "doc_aggs": [{"doc_id": "doc-keep"}, {"doc_id": "doc-drop"}],
        },
        ["doc-keep"],
    )
    assert [item["content"] for item in result["chunks"]] == ["allowed"]
    assert result["doc_aggs"] == [{"doc_id": "doc-keep"}]


def test_scope_filter_empty_allowed_drops_all():
    result = Retrieval._filter_kbinfos_to_scope(
        {
            "chunks": [{"doc_id": "doc-a", "content": "x"}],
            "doc_aggs": [{"doc_id": "doc-a"}],
        },
        [],
    )
    assert result["chunks"] == []
    assert result["doc_aggs"] == []
