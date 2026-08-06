"""Real parsing evaluation collector: S3 -> Gateway -> RAGFlow public API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from enterprise.gateway.query.ragflow_client import RAGFlowQueryClient
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
)
from enterprise.scripts.wp03.metrics import compute_document_metrics
from enterprise.scripts.wp03.quality_gate import evaluate_document_quality

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES = {
    "digital_text",
    "clear_scan",
    "degraded_scan",
    "mixed_manual",
    "table_dense",
    "diagram",
    "zh_en_mixed",
}


@dataclass
class EvaluationConfig:
    gateway_url: str = field(
        default_factory=lambda: os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188")
    )
    ragflow_base_url: str = field(
        default_factory=lambda: os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380")
    )
    ragflow_api_key: str = field(
        default_factory=lambda: os.environ.get("RAGFLOW_API_KEY", "").strip()
    )
    service_token: str = field(
        default_factory=lambda: os.environ.get(
            "ENTERPRISE_SYNC_SERVICE_TOKEN", ""
        ).strip()
    )
    s3_endpoint: str = field(
        default_factory=lambda: os.environ.get("S3_ENDPOINT", "").strip()
    )
    s3_access_key: str = field(
        default_factory=lambda: os.environ.get("S3_ACCESS_KEY", "").strip()
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.environ.get("S3_SECRET_KEY", "").strip()
    )
    s3_bucket: str = field(
        default_factory=lambda: os.environ.get("S3_BUCKET", "wp03-eval").strip()
    )
    tenant_id: str = field(
        default_factory=lambda: os.environ.get("WP03_TENANT", "wp03-eval")
    )
    source_system: str = "WP03"
    source_version_id: str = "v1"
    timeout_seconds: int = 900
    skip_citations: bool = False
    fresh_parse: bool = False


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("sample manifest schema_version must be 1")
    provenance = manifest.get("ground_truth_provenance")
    if not isinstance(provenance, dict) or not provenance.get("source"):
        raise ValueError("sample manifest must declare ground_truth_provenance.source")
    if not isinstance(provenance.get("human_reviewed"), bool):
        raise ValueError("sample manifest ground_truth_provenance.human_reviewed must be bool")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("sample manifest must contain at least one sample")
    ids = [s.get("sample_id") for s in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("sample_id values must be unique")
    for sample in samples:
        if sample.get("category") not in ALLOWED_CATEGORIES:
            raise ValueError(f"unknown category: {sample.get('category')}")
        if int(sample.get("pages", 0)) <= 0:
            raise ValueError(f"invalid page count: {sample.get('sample_id')}")
        if not isinstance(sample.get("ground_truth_fields"), dict):
            raise ValueError(
                f"missing ground_truth_fields: {sample.get('sample_id')}"
            )


def load_manifest(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    validate_manifest(manifest)
    return manifest


class S3Store:
    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config

    def _client(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.config.s3_endpoint,
            aws_access_key_id=self.config.s3_access_key,
            aws_secret_access_key=self.config.s3_secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    def ensure_bucket(self) -> None:
        client = self._client()
        try:
            client.head_bucket(Bucket=self.config.s3_bucket)
        except Exception:
            client.create_bucket(Bucket=self.config.s3_bucket)

    def put(self, object_key: str, content: bytes) -> None:
        import io

        self.ensure_bucket()
        self._client().upload_fileobj(
            io.BytesIO(content),
            self.config.s3_bucket,
            object_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )


class ParsingEvaluationCollector:
    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config
        self.store = S3Store(config)

    def _gateway_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.service_token:
            headers["Authorization"] = f"Bearer {self.config.service_token}"
        return headers

    def _payload_for(self, sample: dict[str, Any], pdf_path: Path, run_id: str) -> dict:
        content = pdf_path.read_bytes()
        fields = sample["ground_truth_fields"]
        external_document_id = f"WP03-{run_id}-{sample['sample_id']}"
        return {
            "eventId": f"{run_id}-{sample['sample_id']}",
            "eventType": "upsert",
            "sourceSystem": self.config.source_system,
            "externalDocumentId": external_document_id,
            "sourceVersionId": self.config.source_version_id,
            "sha256": hashlib.sha256(content).hexdigest(),
            "fileName": sample["file_name"],
            "mediaType": "application/pdf",
            "source": {
                "bucket": self.config.s3_bucket,
                "objectKey": f"wp03/{run_id}/{sample['file_name']}",
            },
            "metadata": {
                "schema_version": 1,
                "tenant_id": self.config.tenant_id,
                "external_document_id": external_document_id,
                "source_system": self.config.source_system,
                "equipment_id": fields["equipment_id"],
                "fixed_asset_no": fields.get("fixed_asset_no"),
                "document_type": fields["document_type"],
                "document_version": fields["version"],
                "department_id": "d10",
                "security_level": 2,
                "business_status": "active",
                "model": fields.get("model"),
                "effective_date": fields.get("effective_date"),
            },
            "batchId": f"wp03-{run_id}",
        }

    def submit_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self.config.gateway_url}/enterprise/api/v1/documents",
                json=payload,
                headers=self._gateway_headers(),
            )
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"gateway document submit failed: {resp.status_code} {resp.text[:300]}"
            )
        return resp.json()

    def wait_document_status(
        self, external_document_id: str, timeout_seconds: int | None = None
    ) -> dict[str, Any]:
        deadline = time.time() + (timeout_seconds or self.config.timeout_seconds)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{self.config.gateway_url}/enterprise/api/v1/documents/"
                    f"{external_document_id}/status",
                    params={
                        "tenant_id": self.config.tenant_id,
                        "source_system": self.config.source_system,
                        "refresh": "true",
                    },
                    headers=self._gateway_headers(),
                )
            if resp.status_code == 200:
                last = resp.json()
                if last.get("status") in ("ready", "failed", "disabled"):
                    return last
            time.sleep(5)
        raise TimeoutError(
            f"document {external_document_id} did not reach terminal status: {last}"
        )

    async def collect_ragflow(
        self, sync_doc: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset_id = sync_doc.get("ragflowDatasetId")
        document_id = sync_doc.get("ragflowDocumentId")
        if not dataset_id or not document_id:
            raise RuntimeError("gateway response missing RAGFlow document ids")
        client = RAGFlowDocumentClient(
            base_url=self.config.ragflow_base_url,
            api_key=self.config.ragflow_api_key,
        )
        docs = await client.list_documents(dataset_id, document_id=document_id)
        if not docs:
            raise RuntimeError(f"RAGFlow document not found: {document_id}")
        doc_info = docs[0]
        chunks: list[dict[str, Any]] = []
        page = 1
        page_size = 100
        while True:
            result = await client.list_chunks(
                dataset_id, document_id, page=page, page_size=page_size
            )
            data = result.get("data") or {}
            batch = data.get("chunks") or []
            chunks.extend(batch)
            total = int(data.get("total") or 0)
            if not batch or len(chunks) >= total or len(batch) < page_size:
                break
            page += 1
        normalized = [
            {
                "id": chunk.get("id"),
                "content": chunk.get("content", ""),
                "document_id": chunk.get("document_id"),
                "dataset_id": chunk.get("dataset_id"),
                "positions": chunk.get("positions") or [],
                "image_id": chunk.get("image_id"),
                "doc_type_kwd": chunk.get("doc_type_kwd"),
                "available": chunk.get("available"),
            }
            for chunk in chunks
        ]
        return doc_info, normalized

    async def collect_citations(
        self,
        dataset_id: str,
        document_id: str,
        citation_questions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if (
            not citation_questions
            or not self.config.ragflow_api_key
            or self.config.skip_citations
        ):
            return None
        client = RAGFlowQueryClient(
            base_url=self.config.ragflow_base_url,
            api_key=self.config.ragflow_api_key,
        )
        chat_name = f"wp03-eval-{self.config.tenant_id}"
        chats = await client.list_chats(name=chat_name)
        chat_id = next(
            (chat.get("id") for chat in chats if chat.get("name") == chat_name),
            None,
        )
        if not chat_id:
            created = await client.create_chat(chat_name, [dataset_id])
            chat_id = created["data"]["id"]
        results: list[dict[str, Any]] = []
        for item in citation_questions:
            expected_page = int(item["expected_page"])
            matched = False
            error = None
            try:
                completion = await client.chat_completion(
                    chat_id,
                    item["question"],
                    doc_ids=[document_id],
                )
                reference = (completion.get("data") or {}).get("reference") or {}
                for chunk in reference.get("chunks") or []:
                    for pos in chunk.get("positions") or []:
                        try:
                            page = int(pos[0])
                        except (TypeError, ValueError, IndexError):
                            continue
                        if page == expected_page:
                            matched = True
                            break
                    if matched:
                        break
            except (RAGFlowAPIError, Exception) as exc:  # noqa: BLE001
                error = type(exc).__name__
            results.append(
                {
                    "question": item["question"],
                    "expected_page": expected_page,
                    "matched": matched,
                    "error": error,
                }
            )
        return results

    async def run_sample(
        self,
        sample: dict[str, Any],
        samples_dir: Path,
        run_id: str,
    ) -> dict[str, Any]:
        pdf_path = samples_dir / sample["file_name"]
        if not pdf_path.exists():
            raise FileNotFoundError(
                f"sample file missing: {pdf_path}; run generate_samples.py first"
            )
        payload = self._payload_for(sample, pdf_path, run_id)
        content = pdf_path.read_bytes()
        await asyncio.to_thread(
            self.store.put,
            payload["source"]["objectKey"],
            content,
        )
        started_at = time.monotonic()
        submit = await asyncio.to_thread(self.submit_document, payload)
        external_id = submit.get("externalDocumentId")
        sync_doc = await asyncio.to_thread(
            self.wait_document_status, external_id
        )
        wall_clock = time.monotonic() - started_at
        sync_status = sync_doc.get("status")
        parse_failed = sync_status == "failed"
        if parse_failed:
            metrics = {
                "document_id": None,
                "dataset_id": sync_doc.get("ragflowDatasetId"),
                "parsing_status": "FAIL",
                "error_code": (
                    (sync_doc.get("error") or {}).get("code")
                    if isinstance(sync_doc.get("error"), dict)
                    else sync_doc.get("error")
                ),
                "parse_success": False,
                "chunk_count": 0,
                "page_count_source": int(sample["pages"]),
                "page_count_observed": 0,
                "wall_clock_duration_seconds": round(wall_clock, 3),
            }
            quality_status, reasons = "failed", ["GATEWAY_SYNC_FAILED"]
            metrics["quality_status"] = quality_status
            return {
                "sample_id": sample["sample_id"],
                "category": sample["category"],
                "sync_status": sync_status,
                "parse_quality_status": quality_status,
                "quality_reasons": reasons,
                "metrics": metrics,
                "chunks": [],
                "error": sync_doc.get("error"),
            }

        doc_info, chunks = await self.collect_ragflow(sync_doc)
        citation_results = await self.collect_citations(
            sync_doc.get("ragflowDatasetId"),
            sync_doc.get("ragflowDocumentId"),
            sample.get("citation_questions"),
        )
        metrics = compute_document_metrics(
            doc_info,
            chunks,
            int(sample["pages"]),
            ground_truth_fields=sample.get("ground_truth_fields"),
            expected_tables=sample.get("expected_tables"),
            wall_clock_duration_seconds=wall_clock,
            citation_results=citation_results,
        )
        citation_expected = bool(sample.get("citation_questions")) and (
            not self.config.skip_citations
        )
        quality_status, reasons = evaluate_document_quality(
            metrics,
            expected_tables=sample.get("expected_tables"),
            ground_truth_fields=sample.get("ground_truth_fields"),
            citation_expected=citation_expected,
        )
        metrics["quality_status"] = quality_status
        return {
            "sample_id": sample["sample_id"],
            "category": sample["category"],
            "sync_status": sync_status,
            "ragflow_dataset_id": sync_doc.get("ragflowDatasetId"),
            "ragflow_document_id": sync_doc.get("ragflowDocumentId"),
            "parse_quality_status": quality_status,
            "quality_reasons": reasons,
            "metrics": metrics,
            "chunks": chunks,
            "citation_results": citation_results,
            "error": None,
        }
