"""Optional RAGFlow contract tests for the query demo closed loop.

These tests only run when ENTERPRISE_RAGFLOW_BASE_URL and
ENTERPRISE_RAGFLOW_API_KEY are configured. They create a unique dataset and
exercise the public API shapes the query router depends on.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.query.ragflow_client import (  # noqa: E402
    RAGFlowQueryClient,
)

BASE_URL = os.environ.get("ENTERPRISE_RAGFLOW_BASE_URL", "")
API_KEY = os.environ.get("ENTERPRISE_RAGFLOW_API_KEY", "")

class TestRAGFlowQueryContract:
    @pytest.mark.asyncio
    async def test_document_parse_chat_and_citation_contract(self):
        if not BASE_URL or not API_KEY:
            pytest.fail("ENTERPRISE_RAGFLOW_BASE_URL/API_KEY are required for Integration")
        client = RAGFlowQueryClient(api_key=API_KEY)
        dataset_name = f"contract-{uuid.uuid4().hex[:12]}"
        chat_name = f"contract-chat-{uuid.uuid4().hex[:12]}"
        dataset_id = ""
        chat_id = ""
        try:
            created = await client.create_dataset(dataset_name)
            dataset_id = created["data"]["id"]

            pdf_path = (
                Path(__file__).resolve().parent
                / "fixtures"
                / "Doc1.pdf"
            )
            uploaded = await client.upload_document(
                dataset_id, pdf_path.name, pdf_path.read_bytes()
            )
            document_id = uploaded["data"][0]["id"]

            parsed = await client.start_parsing(dataset_id, [document_id])
            assert parsed.get("code") == 0

            done = False
            for _ in range(24):
                docs = await client.list_documents(
                    dataset_id, document_id=document_id
                )
                assert isinstance(docs, list)
                if docs and docs[0].get("run") == "DONE":
                    done = True
                    break
                await asyncio.sleep(5)
            assert done, "document did not reach DONE in time"

            chat = await client.create_chat(chat_name, [dataset_id])
            chat_id = chat["data"]["id"]

            completion = await client.chat_completion(
                chat_id,
                "What is RAGFlow?",
                doc_ids=[document_id],
            )
            data = completion["data"]
            assert data.get("answer")
            reference = data.get("reference") or {}
            chunks = reference.get("chunks") or []
            assert chunks, "chat completion returned no reference chunks"
            assert all(
                chunk.get("id") and chunk.get("document_id")
                for chunk in chunks
            )
            assert all(
                chunk.get("document_id") == document_id
                for chunk in chunks
            ), "doc_ids scope leaked chunks from another document"
        finally:
            if chat_id:
                await client.delete_chat(chat_id)
            if dataset_id:
                await client.delete_dataset(dataset_id)
