"""RAGFlow client additions used by the query demo router."""
import asyncio
import json
import logging
import os
from typing import Any
import uuid

from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
)

logger = logging.getLogger(__name__)


def _trace_doc_ids(request_id: str, doc_ids: list[str] | None) -> None:
    """Record the exact doc_ids sent to RAGFlow for E2E verification."""
    path = os.environ.get("ENTERPRISE_QUERY_TRACE_DOC_IDS", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"requestId": request_id, "docIds": list(doc_ids or [])},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        logger.warning(
            "RAGFlow doc_ids trace unavailable request_id=%s", request_id
        )


class RAGFlowQueryClient(RAGFlowDocumentClient):
    """Public-API client for dataset parsing, chat and completion."""

    @staticmethod
    def _require_ok(result: dict) -> dict:
        if isinstance(result, dict) and result.get("code") not in (0, None):
            raise RAGFlowAPIError(
                str(result.get("message") or "RAGFlow returned an error"), 200
            )
        return result

    async def start_parsing(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents/parse",
            rid,
            json_data={"document_ids": document_ids},
        )
        return self._require_ok(result)

    async def list_chats(
        self, name: str | None = None, request_id: str | None = None
    ) -> list[dict]:
        rid = request_id or self._new_request_id()
        path = "/api/v1/chats"
        if name:
            from urllib.parse import quote

            path += f"?name={quote(name)}"
        result = await self._run_sync(
            self._sync_request, "GET", path, rid
        )
        result = self._require_ok(result)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        return data.get("chats", []) if isinstance(data, dict) else []

    async def create_chat(
        self,
        name: str,
        dataset_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chats",
            rid,
            json_data={"name": name, "dataset_ids": dataset_ids},
        )
        return self._require_ok(result)

    async def delete_chat(
        self,
        chat_id: str,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        return self._require_ok(
            await self._run_sync(
                self._sync_request,
                "DELETE",
                f"/api/v1/chats/{chat_id}",
                rid,
            )
        )

    async def update_chat(
        self,
        chat_id: str,
        dataset_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {}
        if dataset_ids is not None:
            body["dataset_ids"] = dataset_ids
        return self._require_ok(
            await self._run_sync(
                self._sync_request,
                "PATCH",
                f"/api/v1/chats/{chat_id}",
                rid,
                json_data=body,
            )
        )

    async def delete_dataset(
        self,
        dataset_id: str,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        return self._require_ok(
            await self._run_sync(
                self._sync_request,
                "DELETE",
                "/api/v1/datasets",
                rid,
                json_data={"ids": [dataset_id]},
            )
        )

    async def get_session(
        self,
        chat_id: str,
        session_id: str,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "GET",
            f"/api/v1/chats/{chat_id}/sessions/{session_id}",
            rid,
        )
        return self._require_ok(result)

    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "stream": False,
        }
        if session_id:
            body["session_id"] = session_id
        if doc_ids:
            # RAGFlow v0.26.4 /chat/completions expects a comma-separated
            # string for doc_ids; a JSON list breaks its attachment parser.
            body["doc_ids"] = ",".join(doc_ids)
        _trace_doc_ids(rid, doc_ids)
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chat/completions",
            rid,
            json_data=body,
        )
        return self._require_ok(result)

    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ):
        """Stream RAGFlow chat completion over the public SSE API.

        Yields parsed ``{"code", "message", "data"}`` payloads; the terminal
        payload has ``data=True``.
        """
        import httpx

        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "stream": True,
        }
        if session_id:
            body["session_id"] = session_id
        if doc_ids:
            body["doc_ids"] = ",".join(doc_ids)
        _trace_doc_ids(rid, doc_ids)
        timeout = httpx.Timeout(self.timeout, connect=self.timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/v1/chat/completions",
                    json=body,
                    headers=self._headers(rid),
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise RAGFlowAPIError(
                            "RAGFlow stream request failed",
                            resp.status_code,
                            rid,
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload_text = line[len("data:"):].strip()
                        if not payload_text:
                            continue
                        try:
                            payload = json.loads(payload_text)
                        except json.JSONDecodeError as e:
                            raise RAGFlowAPIError(
                                "RAGFlow returned a malformed SSE payload",
                                502,
                                rid,
                            ) from e
                        if not isinstance(payload, dict):
                            continue
                        code = payload.get("code")
                        if code not in (0, None):
                            raise RAGFlowAPIError(
                                str(
                                    payload.get("message")
                                    or "RAGFlow stream error"
                                ),
                                200,
                                rid,
                            )
                        yield payload
            except httpx.HTTPError as e:
                logger.warning(
                    "RAGFlow stream transport error request_id=%s error_type=%s",
                    rid,
                    type(e).__name__,
                )
                raise RAGFlowAPIError(
                    "RAGFlow API request failed", 0, rid
                ) from e


class RAGFlowQueryStub(RAGFlowDocumentStub):
    """Offline stub for the query demo loop."""

    def __init__(self) -> None:
        super().__init__()
        self._chats: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._extra_chunks: list[dict] = []
        self._last_completion_body: dict | None = None
        self._ignore_doc_scope = False
        self._no_evidence = False
        self._empty_answer = False
        self._empty_chunks = False
        self._omit_default_chunk = False
        self._fail_session_read = False
        self._stream_delay = 0.0
        self._stream_fail_after = 0
        self._omit_stream_id = False

    async def start_parsing(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        return {"code": 0, "data": True}

    async def list_chats(
        self, name: str | None = None, request_id: str | None = None
    ) -> list[dict]:
        if name is None:
            return list(self._chats.values())
        return [c for c in self._chats.values() if c.get("name") == name]

    async def create_chat(
        self,
        name: str,
        dataset_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        chat = {
            "id": f"chat-{uuid.uuid4().hex[:12]}",
            "name": name,
            "dataset_ids": dataset_ids,
        }
        self._chats[chat["id"]] = chat
        return {"code": 0, "data": chat}

    async def delete_chat(
        self,
        chat_id: str,
        request_id: str | None = None,
    ) -> dict:
        self._chats.pop(chat_id, None)
        return {"code": 0, "data": True}

    async def update_chat(
        self,
        chat_id: str,
        dataset_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        chat = self._chats.get(chat_id)
        if chat is None:
            raise RAGFlowAPIError("Stub: chat not found", 404)
        if dataset_ids is not None:
            chat["dataset_ids"] = dataset_ids
        return {"code": 0, "data": chat}

    async def delete_dataset(
        self,
        dataset_id: str,
        request_id: str | None = None,
    ) -> dict:
        self._datasets.pop(dataset_id, None)
        return {"code": 0, "data": True}

    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        turn_id = f"msg-{uuid.uuid4().hex[:12]}"
        base_chunk = {
            "id": "chunk-1",
            "content": "故障码 E-104 时先检查液压油位。",
            "document_id": "doc-1",
            "document_name": "manual.pdf",
            "positions": [[3, 0.1, 0.2, 0.8, 0.4]],
        }
        if self._no_evidence:
            session_id = session_id or "stub-session"
            self._append_no_evidence_turn(
                session_id, question, turn_id
            )
            return {
                "code": 0,
                "data": {
                    "answer": "",
                    "id": turn_id,
                    "session_id": session_id,
                    "reference": {"chunks": []},
                },
            }
        if self._empty_answer:
            session_id = session_id or "stub-session"
            self._append_no_evidence_turn(
                session_id, question, turn_id, chunks=[base_chunk]
            )
            return {
                "code": 0,
                "data": {
                    "answer": "",
                    "id": turn_id,
                    "session_id": session_id,
                    "reference": {"chunks": [base_chunk]},
                },
            }
        if self._empty_chunks:
            session_id = session_id or "stub-session"
            self._append_no_evidence_turn(
                session_id, question, turn_id
            )
            return {
                "code": 0,
                "data": {
                    "answer": "stub answer",
                    "id": turn_id,
                    "session_id": session_id,
                    "reference": {"chunks": []},
                },
            }
        self._last_completion_body = {
            "chat_id": chat_id,
            "question": question,
            "session_id": session_id,
            "doc_ids": ",".join(doc_ids) if doc_ids else None,
        }
        session_id = session_id or "stub-session"
        session = self._sessions.setdefault(
            session_id, {"messages": [], "reference": []}
        )
        chunks = [
            base_chunk
        ] + list(self._extra_chunks)
        if self._omit_default_chunk:
            chunks = list(self._extra_chunks)
        if doc_ids and not self._ignore_doc_scope:
            allowed = set(doc_ids)
            chunks = [c for c in chunks if c.get("document_id") in allowed]
        session["messages"].append(
            {"role": "user", "content": question, "id": turn_id}
        )
        session["messages"].append(
            {
                "role": "assistant",
                "content": f"stub answer for: {question}",
                "id": turn_id,
            }
        )
        session["reference"].append({"chunks": chunks})
        return {
            "code": 0,
            "data": {
                "answer": f"stub answer for: {question}",
                "id": turn_id,
                "session_id": session_id,
                "reference": {"chunks": chunks},
            },
        }

    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
    ):
        if self._stream_fail_after == 0:
            completion = await self.chat_completion(
                chat_id,
                question,
                session_id=session_id,
                doc_ids=doc_ids,
                request_id=request_id,
            )
            data = completion.get("data", {})
            if isinstance(data, dict) and data.get("answer"):
                await asyncio.sleep(self._stream_delay)
                stream_id = None if self._omit_stream_id else data.get("id")
                yield {
                    "code": 0,
                    "message": "",
                    "data": {
                        "answer": data.get("answer", ""),
                        "id": stream_id,
                        "session_id": data.get("session_id"),
                    },
                }
            stream_id = None if self._omit_stream_id else data.get("id")
            yield {
                "code": 0,
                "message": "",
                "data": {
                    "answer": "",
                    "id": stream_id,
                    "session_id": data.get("session_id"),
                    "reference": data.get("reference", {}),
                    "final": True,
                },
            }
            yield {"code": 0, "message": "", "data": True}
            return
        self._stream_fail_after -= 1
        raise RAGFlowAPIError("Stub: stream failed mid-flight", 503)

    def _append_no_evidence_turn(
        self,
        session_id: str,
        question: str,
        turn_id: str,
        *,
        chunks: list[dict] | None = None,
    ) -> None:
        session = self._sessions.setdefault(
            session_id, {"messages": [], "reference": []}
        )
        session["messages"].append(
            {"role": "user", "content": question, "id": turn_id}
        )
        session["messages"].append(
            {
                "role": "assistant",
                "content": "未找到与问题相关的可靠依据。",
                "id": turn_id,
            }
        )
        session["reference"].append({"chunks": chunks or []})

    async def get_session(
        self,
        chat_id: str,
        session_id: str,
        request_id: str | None = None,
    ) -> dict:
        if self._fail_session_read:
            raise RAGFlowAPIError("Stub: session read failed", 503)
        return {
            "code": 0,
            "data": self._sessions.get(
                session_id, {"messages": [], "reference": []}
            ),
        }
