"""Optional live RAGFlow contract test for WP-03 chunk collection.

Runs only when ENTERPRISE_RAGFLOW_BASE_URL and ENTERPRISE_RAGFLOW_API_KEY are
configured. It uses the public chunk API and never touches RAGFlow internals.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.sync.ragflow_document_client import (  # noqa: E402
    RAGFlowDocumentClient,
)

BASE_URL = os.environ.get("ENTERPRISE_RAGFLOW_BASE_URL", "")
API_KEY = os.environ.get("ENTERPRISE_RAGFLOW_API_KEY", "")
SKIP_REASON = (
    "ENTERPRISE_RAGFLOW_BASE_URL/API_KEY not configured"
    if not BASE_URL or not API_KEY
    else ""
)


@pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
class TestWP03RAGFlowContract:
    @pytest.mark.asyncio
    async def test_list_chunks_returns_public_shape(self):
        client = RAGFlowDocumentClient(
            base_url=BASE_URL,
            api_key=API_KEY,
        )
        dataset_name = f"wp03-contract-{uuid.uuid4().hex[:12]}"
        dataset_id = ""
        document_id = ""
        try:
            created = await client.create_dataset(dataset_name)
            dataset_id = created["data"]["id"]
            pdf_path = (
                Path(__file__).resolve().parents[2]
                / "ragflow"
                / "test"
                / "benchmark"
                / "test_docs"
                / "Doc1.pdf"
            )
            uploaded = await client.upload_document(
                dataset_id, pdf_path.name, pdf_path.read_bytes()
            )
            document_id = uploaded["data"][0]["id"]
            await client.start_parsing(dataset_id, [document_id])
            done = False
            for _ in range(24):
                docs = await client.list_documents(
                    dataset_id, document_id=document_id
                )
                if docs and docs[0].get("run") == "DONE":
                    done = True
                    break
                await asyncio.sleep(5)
            assert done, "document did not reach DONE"
            result = await client.list_chunks(
                dataset_id, document_id, page=1, page_size=100
            )
            data = result.get("data") or {}
            assert "chunks" in data
            assert "total" in data
            for chunk in data.get("chunks", []):
                assert "id" in chunk
                assert "content" in chunk
                assert "document_id" in chunk
        finally:
            if dataset_id and document_id:
                await client.delete_documents(dataset_id, [document_id])
