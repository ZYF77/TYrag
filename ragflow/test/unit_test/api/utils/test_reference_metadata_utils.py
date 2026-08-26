#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

from api.utils.reference_metadata_utils import (
    _resolve_chunk_doc_id,
    enrich_chunks_with_document_metadata,
)


def test_resolve_chunk_doc_id_falls_back_to_document_id():
    assert _resolve_chunk_doc_id({"doc_id": "a", "document_id": "b"}) == "a"
    assert _resolve_chunk_doc_id({"document_id": "b"}) == "b"
    assert _resolve_chunk_doc_id({}) is None


def test_enrich_chunks_with_document_id_fallback(monkeypatch):
    """Chunks that only carry document_id still receive document_metadata."""
    calls = []

    def _fake_getter(doc_ids, kb_id):
        calls.append((list(doc_ids), kb_id))
        return {
            "doc-only-document-id": {
                "equipment_id": "GQ01250024",
                "fixed_asset_no": "FA-1",
            }
        }

    monkeypatch.setattr(
        "api.db.services.doc_metadata_service.DocMetadataService.get_metadata_for_documents",
        _fake_getter,
    )

    chunks = [
        {
            "kb_id": "kb-1",
            "document_id": "doc-only-document-id",
            "content_with_weight": "certificate body",
        }
    ]
    enrich_chunks_with_document_metadata(
        chunks,
        {"equipment_id", "fixed_asset_no"},
    )

    assert calls == [(["doc-only-document-id"], "kb-1")]
    assert chunks[0]["document_metadata"] == {
        "equipment_id": "GQ01250024",
        "fixed_asset_no": "FA-1",
    }
