"""Extended RAGFlow client: dataset and document operations for WP-02A.
Uses synchronous urllib calls via thread executor for FastAPI compatibility."""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from enterprise.gateway.config import config

logger = logging.getLogger(__name__)


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
            from urllib.parse import urlencode
            import io
            boundary = "----FormBoundary" + uuid.uuid4().hex
            body_parts = []
            for name, (fname, fobj, ctype) in files.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode())
                body_parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
                body_parts.append(fobj.read() if hasattr(fobj, 'read') else fobj)
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
            err_body = e.read().decode() if e.fp else ""
            raise RAGFlowAPIError(f"HTTP {e.code}: {err_body}", e.code, request_id)
        except Exception as e:
            raise RAGFlowAPIError(str(e), 0, request_id)

    async def _run_sync(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
        

    async def create_dataset(self, name: str, description: str = "",
                             request_id: str | None = None) -> dict:
        rid = request_id or self._new_request_id()
        return await self._run_sync(self._sync_request, "POST", "/api/v1/datasets",
                                     rid, json_data={"name": name, "description": description})

    async def list_datasets(self, request_id: str | None = None) -> list[dict]:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(self._sync_request, "GET", "/api/v1/datasets", rid)
        return result.get("data", []) if isinstance(result, dict) else result

    async def upload_document(self, dataset_id: str, file_name: str,
                              file_content: bytes, request_id: str | None = None) -> dict:
        rid = request_id or self._new_request_id()
        import io
        files = {"file": (file_name, io.BytesIO(file_content), "application/octet-stream")}
        return await self._run_sync(self._sync_request, "POST",
                                     f"/api/v1/datasets/{dataset_id}/documents",
                                     rid, files=files)

    async def list_documents(self, dataset_id: str,
                             request_id: str | None = None) -> list[dict]:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(self._sync_request, "GET",
                                       f"/api/v1/datasets/{dataset_id}/documents", rid)
        return result.get("data", []) if isinstance(result, dict) else result


# Stub for testing
class RAGFlowDocumentStub(RAGFlowDocumentClient):
    def __init__(self):
        super().__init__(base_url="stub://test", api_key="stub-key")
        self._datasets: dict[str, dict] = {}
        self._documents: dict[str, dict] = {}
        self._next_id = 1
        self._fail_next = False

    async def create_dataset(self, name: str, description: str = "",
                             request_id: str | None = None) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        ds_id = f"ds-{self._next_id}"; self._next_id += 1
        ds = {"data": {"id": ds_id, "name": name}}
        self._datasets[ds_id] = ds
        return ds

    async def list_datasets(self, request_id: str | None = None) -> list[dict]:
        return [v["data"] for v in self._datasets.values()]

    async def upload_document(self, dataset_id: str, file_name: str,
                              file_content: bytes, request_id: str | None = None) -> dict:
        if self._fail_next:
            raise RAGFlowAPIError("Stub: simulated RAGFlow failure", 503)
        doc_id = f"doc-{self._next_id}"; self._next_id += 1
        doc = {"data": [{"id": doc_id, "name": file_name, "dataset_id": dataset_id,
                         "run": "UNSTART", "chunk_method": "naive"}]}
        self._documents[doc_id] = doc
        return doc
