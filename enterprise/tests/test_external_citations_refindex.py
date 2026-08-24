"""v2 external citations must expose answer marker refIndex."""

from __future__ import annotations

from enterprise.gateway.query.formal_router import _validate_chunks
from enterprise.gateway.query.v2_router import _external_citations
from enterprise.gateway.sync.models import ExtDocumentMap


def _doc(ragflow_document_id: str, external_document_id: str) -> ExtDocumentMap:
    return ExtDocumentMap(
        tenant_id="t1",
        source_system="eam",
        external_document_id=external_document_id,
        source_version_id="v1",
        event_id="evt-1",
        sha256="a" * 64,
        file_name=f"{external_document_id}.pdf",
        ragflow_document_id=ragflow_document_id,
        asset_id="FA-1",
    )


def test_external_citations_carry_answer_marker_ref_index():
    chunks = [
        {"id": "c0", "document_id": "rf-0", "content": "invoice"},
        {"id": "c1", "doc_id": "rf-1", "content": "repair"},
        {"id": "c2", "document_id": "rf-2", "content": "manual"},
    ]
    docs = {
        "rf-0": _doc("rf-0", "EXT-0"),
        "rf-1": _doc("rf-1", "EXT-1"),
        "rf-2": _doc("rf-2", "EXT-2"),
    }

    citations = _external_citations(
        chunks,
        docs,
        message_id="msg-12345678",
        answer="手册 [ID:2] 与工单 [ID:1]",
        status="completed",
    )

    assert [c["refIndex"] for c in citations] == [2, 1]
    assert [c["documentId"] for c in citations] == ["EXT-2", "EXT-1"]
    assert citations[0]["title"].endswith("EXT-2.pdf") or "EXT-2" in citations[0]["title"]


def test_official_doc_id_alias_remains_inside_scope():
    _validate_chunks([{"id": "c1", "doc_id": "rf-1"}], {"rf-1": _doc("rf-1", "EXT-1")})
