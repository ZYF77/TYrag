"""Extended RAGFlow client: dataset and document operations for WP-02A."""
import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import httpx

from enterprise.gateway.config import config

logger = logging.getLogger(__name__)

_SENSITIVE_LOG_TERMS = (
    "authorization",
    "apikey",
    "token",
    "password",
    "secret",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[-_\s]", "", key).lower()
    return any(term in normalized for term in _SENSITIVE_LOG_TERMS)


def _redact_json_string(match: re.Match[str]) -> str:
    key, value = match.group(1), match.group(2)
    if _is_sensitive_key(key):
        return f'"{key}": "<redacted>"'
    return match.group(0)


def _redact_query_param(match: re.Match[str]) -> str:
    key = match.group(1)
    if _is_sensitive_key(key):
        return f"{key}=<redacted>"
    return match.group(0)


def sanitize_log_payload(payload: str | bytes, limit: int = 1000) -> str:
    """Return a truncated, redacted copy of a raw payload for logs only."""
    text = payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload)
    text = re.sub(
        r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"',
        _redact_json_string,
        text,
    )
    text = re.sub(
        r"([A-Za-z_][A-Za-z0-9_-]*)=([^\s&]+)",
        _redact_query_param,
        text,
    )
    return text[:limit]


class RAGFlowAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class RAGFlowDocumentClient:
    """Document-level operations against RAGFlow public API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url or config.ragflow_base_url
        self.api_key = api_key or "stub-key"
        self.timeout = config.ragflow_timeout

    def _headers(self, request_id: str | None = None) -> dict:
        import urllib.request
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if request_id:
            h["X-Request-ID"] = request_id
        return h

    def _new_request_id(self) -> str:
        return str(uuid.uuid4())

    def _sync_request(self, method: str, path: str, request_id: str,
                      json_data: dict | None = None,
                      files: dict | None = None) -> dict:
        import urllib.request, urllib.error
        url = f"{self.base_url}{path}"
        headers = self._headers(request_id)
        body = None

        if files:
            # Chat attachments still upload via multipart on _sync_request;
            # dataset docs use _sync_upload_document (httpx) instead.
            boundary = "----FormBoundary" + uuid.uuid4().hex
            body_parts = []
            for name, (fname, fobj, ctype) in files.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{fname}"\r\n'.encode()
                )
                body_parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
                body_parts.append(fobj.read() if hasattr(fobj, "read") else fobj)
                body_parts.append(b"\r\n")
            body_parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(body_parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif json_data:
            body = json.dumps(json_data).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace") if e.fp else ""
            logger.warning(
                "RAGFlow HTTP %s request_id=%s response sanitized: %s",
                e.code,
                request_id,
                sanitize_log_payload(err_body),
            )
            raise RAGFlowAPIError(
                "RAGFlow API request failed", e.code, request_id
            ) from e
        except Exception as e:
            logger.warning(
                "RAGFlow request failed request_id=%s error_type=%s",
                request_id,
                type(e).__name__,
            )
            raise RAGFlowAPIError("RAGFlow API request failed", 0, request_id) from e

    def _sync_upload_document(
        self,
        dataset_id: str,
        file_name: str,
        file_content: bytes | BinaryIO,
        request_id: str,
    ) -> dict:
        headers = self._headers(request_id)
        headers.pop("Content-Type", None)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/api/v1/datasets/{dataset_id}/documents",
                    headers=headers,
                    files={
                        "file": (
                            file_name,
                            file_content,
                            "application/octet-stream",
                        )
                    },
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "RAGFlow HTTP %s request_id=%s response sanitized: %s",
                exc.response.status_code,
                request_id,
                sanitize_log_payload(exc.response.text),
            )
            raise RAGFlowAPIError(
                "RAGFlow API request failed",
                exc.response.status_code,
                request_id,
            ) from exc
        except Exception as exc:
            logger.warning(
                "RAGFlow request failed request_id=%s error_type=%s",
                request_id,
                type(exc).__name__,
            )
            raise RAGFlowAPIError("RAGFlow API request failed", 0, request_id) from exc

    def _sync_request_bytes(
        self, method: str, path: str, request_id: str
    ) -> tuple[bytes, str]:
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        headers = self._headers(request_id)
        headers.pop("Content-Type", None)
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("Content-Type") or "application/octet-stream"
                return resp.read(), content_type.split(";", 1)[0].strip()
        except urllib.error.HTTPError as exc:
            raise RAGFlowAPIError(
                "RAGFlow API request failed", exc.code, request_id
            ) from exc
        except Exception as exc:
            raise RAGFlowAPIError("RAGFlow API request failed", 0, request_id) from exc

    async def _run_sync(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
        

    async def create_dataset(self, name: str, description: str = "",
                             request_id: str | None = None,
                             permission: str | None = None) -> dict:
        rid = request_id or self._new_request_id()
        payload = {"name": name, "description": description}
        if permission is not None:
            payload["permission"] = permission
        return await self._run_sync(self._sync_request, "POST", "/api/v1/datasets",
                                     rid, json_data=payload)

    async def list_datasets(self, request_id: str | None = None) -> list[dict]:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(self._sync_request, "GET", "/api/v1/datasets", rid)
        return result.get("data", []) if isinstance(result, dict) else result

    async def delete_dataset(
        self, dataset_id: str, request_id: str | None = None
    ) -> dict:
        """Delete a temporary dataset through the public API."""
        rid = request_id or self._new_request_id()
        return await self._run_sync(
            self._sync_request,
            "DELETE",
            "/api/v1/datasets",
            rid,
            json_data={"ids": [dataset_id]},
        )

    async def upload_document(self, dataset_id: str, file_name: str,
                              file_content: bytes | BinaryIO,
                              request_id: str | None = None) -> dict:
        rid = request_id or self._new_request_id()
        return await self._run_sync(
            self._sync_upload_document,
            dataset_id,
            file_name,
            file_content,
            rid,
        )

    async def start_parsing(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        """Trigger the public RAGFlow parse API for registered documents."""
        rid = request_id or self._new_request_id()
        return await self._run_sync(
            self._sync_request,
            "POST",
            f"/api/v1/datasets/{dataset_id}/chunks",
            rid,
            json_data={"document_ids": document_ids},
        )

    async def list_documents(
        self,
        dataset_id: str,
        document_id: str | None = None,
        page: int = 1,
        page_size: int = 100,
        request_id: str | None = None,
    ) -> list[dict]:
        rid = request_id or self._new_request_id()
        from urllib.parse import quote

        path = (
            f"/api/v1/datasets/{dataset_id}/documents"
            f"?page={page}&page_size={page_size}"
        )
        if document_id:
            path += f"&id={quote(document_id)}"
        result = await self._run_sync(self._sync_request, "GET",
                                       path, rid)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        return data.get("docs", []) if isinstance(data, dict) else []

    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        page: int = 1,
        page_size: int = 30,
        request_id: str | None = None,
    ) -> dict:
        """List chunks for a document through the RAGFlow public API."""
        rid = request_id or self._new_request_id()
        path = (
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks"
            f"?page={page}&page_size={page_size}"
        )
        return await self._run_sync(self._sync_request, "GET", path, rid)

    async def update_document(
        self,
        dataset_id: str,
        document_id: str,
        *,
        meta_fields: dict | None = None,
        enabled: bool | None = None,
        chunk_method: str | None = None,
        parser_config: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict = {}
        if meta_fields is not None:
            body["meta_fields"] = meta_fields
        if enabled is not None:
            body["enabled"] = 1 if enabled else 0
        if chunk_method is not None:
            body["chunk_method"] = chunk_method
        if parser_config is not None:
            body["parser_config"] = parser_config
        return await self._run_sync(
            self._sync_request, "PATCH",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
            rid, json_data=body,
        )

    async def update_document_metadata(
        self,
        dataset_id: str,
        document_id: str,
        meta_fields: dict,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict:
        return await self.update_document(
            dataset_id,
            document_id,
            meta_fields=meta_fields,
            enabled=enabled,
            request_id=request_id,
        )

    async def delete_documents(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        return await self._run_sync(
            self._sync_request, "DELETE",
            f"/api/v1/datasets/{dataset_id}/documents",
            rid, json_data={"ids": document_ids},
        )

    async def batch_update_status(
        self,
        dataset_id: str,
        document_ids: list[str],
        enabled: bool,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        return await self._run_sync(
            self._sync_request, "POST",
            f"/api/v1/datasets/{dataset_id}/documents/batch-update-status",
            rid, json_data={"doc_ids": document_ids, "status": "1" if enabled else "0"},
        )

    async def find_or_create_dataset(
        self,
        name: str,
        request_id: str | None = None,
        permission: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        datasets = await self.list_datasets(rid)
        for ds in datasets:
            if ds.get("name") == name:
                return ds
        return await self.create_dataset(
            name,
            request_id=rid,
            permission=permission,
        )


# Stub for testing
class RAGFlowDocumentStub(RAGFlowDocumentClient):
    def __init__(self):
        super().__init__(base_url="stub://test", api_key="stub-key")
        self._datasets: dict[str, dict] = {}
        self._documents: dict[str, dict] = {}
        self._next_id = 1
        self._fail_next = False
        self._fail_metadata_next = False
        self.run_status = "UNSTART"
        self._status_updates: list[tuple[str, list[str], bool]] = []
        self._deleted: list[str] = []
        self._parse_calls: list[tuple[str, list[str]]] = []
        self._operation_log: list[str] = []

    async def create_dataset(self, name: str, description: str = "",
                             request_id: str | None = None,
                             permission: str | None = None) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        ds_id = f"ds-{self._next_id}"; self._next_id += 1
        data = {"id": ds_id, "name": name}
        if permission is not None:
            data["permission"] = permission
        ds = {"data": data}
        self._datasets[ds_id] = ds
        return ds

    async def list_datasets(self, request_id: str | None = None) -> list[dict]:
        return [v["data"] for v in self._datasets.values()]

    async def delete_dataset(
        self, dataset_id: str, request_id: str | None = None
    ) -> dict:
        self._datasets.pop(dataset_id, None)
        return {"code": 0, "data": True}

    async def upload_document(self, dataset_id: str, file_name: str,
                              file_content: bytes | BinaryIO,
                              request_id: str | None = None) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        doc_id = f"doc-{self._next_id}"; self._next_id += 1
        doc = {"data": [{"id": doc_id, "name": file_name, "dataset_id": dataset_id,
                          "run": "UNSTART", "chunk_method": "naive",
                          "parser_config": {}, "meta_fields": {
                              "enterprise_quality_expected_tables": [],
                              "enterprise_quality_ground_truth_fields": {},
                              "enterprise_quality_citation_expected": False,
                              "enterprise_quality_required_capabilities": [
                                  "text", "position",
                              ],
                          }, "enabled": 1}]}
        self._documents[doc_id] = doc
        self._operation_log.append("upload")
        return doc

    async def start_parsing(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        self._parse_calls.append((dataset_id, list(document_ids)))
        self._operation_log.append("parse")
        for doc_id in document_ids:
            doc = self._documents.get(doc_id)
            if doc and doc["data"][0].get("run") in (
                None, "", "UNSTART", "0",
            ):
                desired = str(self.run_status or "UNSTART").upper()
                doc["data"][0]["run"] = (
                    desired if desired in ("DONE", "3", "FAIL", "4") else "RUNNING"
                )
        return {"code": 0, "data": True}

    async def list_documents(
        self,
        dataset_id: str,
        document_id: str | None = None,
        page: int = 1,
        page_size: int = 100,
        request_id: str | None = None,
    ) -> list[dict]:
        self._operation_log.append("get")
        docs = [
            doc["data"][0]
            for doc in self._documents.values()
            if doc["data"][0].get("dataset_id") == dataset_id
        ]
        if document_id:
            docs = [doc for doc in docs if doc.get("id") == document_id]
        return docs

    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        page: int = 1,
        page_size: int = 30,
        request_id: str | None = None,
    ) -> dict:
        del dataset_id, page, page_size, request_id
        doc = self._documents.get(document_id)
        run = ""
        if doc and doc.get("data"):
            run = str(doc["data"][0].get("run") or "").upper()
        # Happy-path stubs that finish as DONE need at least one usable chunk;
        # empty-result retry tests override this method explicitly.
        chunks = []
        if run in {"DONE", "3"}:
            chunks = [{"id": f"{document_id}-c1", "content": "usable stub chunk"}]
        return {
            "code": 0,
            "data": {
                "total": len(chunks),
                "chunks": chunks,
                "doc": {},
            },
        }

    async def update_document(
        self,
        dataset_id: str,
        document_id: str,
        *,
        meta_fields: dict | None = None,
        enabled: bool | None = None,
        chunk_method: str | None = None,
        parser_config: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        if self._fail_next or self._fail_metadata_next:
            self._fail_metadata_next = False
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        doc = self._documents.get(document_id)
        if not doc:
            raise RAGFlowAPIError("Stub: document not found", 404)
        if doc["data"][0].get("dataset_id") != dataset_id:
            raise RAGFlowAPIError("Stub: document not in dataset", 400)
        if meta_fields is not None:
            doc["data"][0]["meta_fields"] = {
                **doc["data"][0].get("meta_fields", {}),
                **meta_fields,
            }
        if enabled is not None:
            doc["data"][0]["enabled"] = 1 if enabled else 0
        if chunk_method is not None:
            doc["data"][0]["chunk_method"] = chunk_method
        if parser_config is not None:
            doc["data"][0]["parser_config"] = {
                **doc["data"][0].get("parser_config", {}),
                **parser_config,
            }
        self._operation_log.append("patch")
        return doc

    async def update_document_metadata(
        self,
        dataset_id: str,
        document_id: str,
        meta_fields: dict,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict:
        return await self.update_document(
            dataset_id,
            document_id,
            meta_fields=meta_fields,
            enabled=enabled,
            request_id=request_id,
        )

    async def delete_documents(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        missing = [did for did in document_ids if did not in self._documents]
        if missing:
            raise RAGFlowAPIError("Stub: documents not found", 404)
        for did in document_ids:
            del self._documents[did]
            self._deleted.append(did)
        return {"data": {"deleted": len(document_ids)}}

    async def batch_update_status(
        self,
        dataset_id: str,
        document_ids: list[str],
        enabled: bool,
        request_id: str | None = None,
    ) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        self._status_updates.append((dataset_id, document_ids, enabled))
        result = {}
        for doc_id in document_ids:
            doc = self._documents.get(doc_id)
            if not doc or doc["data"][0].get("dataset_id") != dataset_id:
                result[doc_id] = {"error": "Document not found"}
                continue
            doc["data"][0]["enabled"] = 1 if enabled else 0
            result[doc_id] = {"status": "1" if enabled else "0"}
        return {"data": result}
