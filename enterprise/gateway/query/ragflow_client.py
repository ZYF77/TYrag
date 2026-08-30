"""RAGFlow client additions used by the query demo router."""
import asyncio
import json
import logging
import os
from typing import Any
import uuid
from urllib.parse import quote

from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
)

logger = logging.getLogger(__name__)

# Mirrors ragflow.dialog_service STANDARD_ABSTAIN_ANSWER / RF-PATCH-007:
# the stub reports the same explicit terminal status the patched upstream sends.
_STANDARD_ABSTAIN_ANSWER = "未找到可靠依据，无法回答。"


def _chunk_stream_text(text: str, size: int = 8) -> list[str]:
    """Split a stub answer into multiple SSE pieces so Gateway can live-stream."""
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]
    return [text[index : index + size] for index in range(0, len(text), size)]


def _json_missing_payload(content: bytes, media_type: str) -> bool:
    """True when RAGFlow returned HTTP 200 JSON ``code: 102`` instead of bytes."""
    lowered = (media_type or "").split(";", 1)[0].strip().lower()
    if lowered not in {"application/json", "text/json", "text/plain"}:
        stripped = content.lstrip()
        if not stripped.startswith(b"{") or b'"code"' not in stripped[:80]:
            return False
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("code") == 102


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
        result = await super().start_parsing(
            dataset_id,
            document_ids,
            request_id=request_id,
        )
        return self._require_ok(result)

    async def get_chunk_evidence(
        self,
        dataset_id: str,
        document_id: str,
        chunk_id: str,
        request_id: str | None = None,
    ) -> dict:
        """Read a single citation chunk through the RAGFlow public API."""
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "GET",
            "/api/v1/datasets/{}/documents/{}/chunks/{}".format(
                quote(dataset_id, safe=""),
                quote(document_id, safe=""),
                quote(chunk_id, safe=""),
            ),
            rid,
        )
        data = self._require_ok(result).get("data") or {}
        if not isinstance(data, dict):
            return {}
        return {
            "id": data.get("id") or data.get("chunk_id") or chunk_id,
            "document_id": data.get("doc_id")
            or data.get("document_id")
            or document_id,
            "content": data.get("content") or data.get("content_with_weight"),
            "positions": data.get("positions") or data.get("position_int") or [],
            "image_id": data.get("image_id") or data.get("img_id") or None,
            "doc_type_kwd": data.get("doc_type_kwd"),
        }

    async def get_document_image(
        self,
        image_id: str,
        request_id: str | None = None,
    ) -> tuple[bytes, str] | None:
        """Fetch a RAGFlow cropped document image without exposing storage paths."""
        if not image_id:
            return None
        rid = request_id or self._new_request_id()
        try:
            content, media_type = await self._run_sync(
                self._sync_request_bytes,
                "GET",
                "/api/v1/documents/images/{}".format(quote(image_id, safe="-_.")),
                rid,
            )
        except RAGFlowAPIError:
            logger.warning("RAGFlow citation image unavailable request_id=%s", rid)
            return None
        if _json_missing_payload(content, media_type):
            logger.warning("RAGFlow citation image unavailable request_id=%s", rid)
            return None
        return content, media_type

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

    async def get_chat(
        self,
        chat_id: str,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = self._require_ok(
            await self._run_sync(
                self._sync_request,
                "GET",
                f"/api/v1/chats/{chat_id}",
                rid,
            )
        )
        data = result.get("data") if isinstance(result, dict) else {}
        return data if isinstance(data, dict) else {}

    async def retrieval(
        self,
        *,
        question: str,
        dataset_ids: list[str],
        document_ids: list[str] | None = None,
        similarity_threshold: float | None = None,
        vector_similarity_weight: float | None = None,
        top_k: int | None = None,
        rerank_id: str | None = None,
        page_size: int | None = None,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "question": question,
            "dataset_ids": list(dataset_ids),
        }
        if document_ids:
            body["document_ids"] = list(document_ids)
        if similarity_threshold is not None:
            body["similarity_threshold"] = similarity_threshold
        if vector_similarity_weight is not None:
            body["vector_similarity_weight"] = vector_similarity_weight
        if top_k is not None:
            body["top_k"] = top_k
        if rerank_id:
            body["rerank_id"] = rerank_id
        if page_size is not None:
            body["page_size"] = page_size
        result = self._require_ok(
            await self._run_sync(
                self._sync_request,
                "POST",
                "/api/v1/retrieval",
                rid,
                json_data=body,
            )
        )
        data = result.get("data") if isinstance(result, dict) else {}
        return data if isinstance(data, dict) else {}

    async def create_chat(
        self,
        name: str,
        dataset_ids: list[str],
        request_id: str | None = None,
        prompt_config: dict | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {"name": name, "dataset_ids": dataset_ids}
        if prompt_config is not None:
            body["prompt_config"] = prompt_config
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chats",
            rid,
            json_data=body,
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
        prompt_config: dict | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {}
        if dataset_ids is not None:
            body["dataset_ids"] = dataset_ids
        if prompt_config is not None:
            body["prompt_config"] = prompt_config
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

    async def create_session(
        self,
        chat_id: str,
        name: str,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "POST",
            f"/api/v1/chats/{chat_id}/sessions",
            rid,
            json_data={"name": name},
        )
        return self._require_ok(result)

    async def chat_completion(
        self,
        chat_id: str | None,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        files: list[dict[str, Any]] | list[str] | None = None,
        internet: bool = False,
        messages: list[dict[str, Any]] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        allowed_identifiers: list[str] | None = None,
        scope_identifiers: list[str] | None = None,
        attachment_observations: list[str] | None = None,
        reasoning: int | None = None,
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "stream": False,
        }
        if messages is not None:
            body["messages"] = [dict(item) for item in messages]
            if files and body["messages"]:
                for item in reversed(body["messages"]):
                    if item.get("role") == "user":
                        item["files"] = list(files)
                        break
        else:
            body["question"] = question
        if chat_id:
            body["chat_id"] = chat_id
        if session_id:
            body["session_id"] = session_id
        if doc_ids:
            # RAGFlow v0.26.4 /chat/completions expects a comma-separated
            # string for doc_ids; a JSON list breaks its attachment parser.
            body["doc_ids"] = ",".join(doc_ids)
        if files and messages is None:
            # RAGFlow expects attachment descriptors
            # ({id, name, mime_type, created_by}), not bare file ids.
            body["files"] = list(files)
        if internet:
            body["internet"] = True
        if store_history_messages is not None:
            body["store_history_messages"] = bool(store_history_messages)
        if pass_all_history_messages is not None:
            body["pass_all_history_messages"] = bool(pass_all_history_messages)
        if grounding_version is not None:
            body["grounding_version"] = grounding_version
        if allowed_identifiers:
            body["allowed_identifiers"] = [str(item) for item in allowed_identifiers if str(item)]
        if scope_identifiers:
            body["scope_identifiers"] = [str(item) for item in scope_identifiers if str(item)]
        if attachment_observations:
            body["attachment_observations"] = [
                str(item) for item in attachment_observations if str(item)
            ]
        if reasoning is not None:
            body["reasoning"] = int(reasoning)
        _trace_doc_ids(rid, doc_ids)
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chat/completions",
            rid,
            json_data=body,
        )
        return self._require_ok(result)

    async def upload_chat_file(
        self,
        file_name: str,
        content: bytes,
        media_type: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload a runtime chat attachment for VLM/OCR understand.

        Must use ``/api/v1/documents/upload`` (not ``/api/v1/files``). Chat
        completion resolves attachments via ``FileService.get_files``, which
        reads the downloads bucket and requires ``id`` / ``mime_type`` /
        ``created_by`` / ``name``.
        """
        import io

        rid = request_id or self._new_request_id()
        files = {"file": (file_name, io.BytesIO(content), media_type)}
        result = self._require_ok(
            await self._run_sync(
                self._sync_request,
                "POST",
                "/api/v1/documents/upload",
                rid,
                files=files,
            )
        )
        data = result.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict) or not data.get("id"):
            raise RAGFlowAPIError("RAGFlow file upload returned no id", 502, rid)
        desc = {
            "id": str(data["id"]),
            "name": str(data.get("name") or file_name),
            "mime_type": str(data.get("mime_type") or media_type),
            "created_by": str(data.get("created_by") or ""),
        }
        if not desc["created_by"]:
            raise RAGFlowAPIError(
                "RAGFlow chat attachment missing created_by", 502, rid
            )
        return desc

    async def delete_file(
        self,
        file_id: str,
        request_id: str | None = None,
        created_by: str | None = None,
    ) -> None:
        """Delete one temporary chat upload through the authenticated API."""
        del created_by
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "DELETE",
            f"/api/v1/documents/upload/{quote(file_id, safe='')}",
            rid,
        )
        self._require_ok(result)

    async def understand_file(
        self,
        chat_id: str | None,
        file: dict[str, Any] | str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Extract short visible facts from an image via vision-only chat.

        Intentionally omits the enterprise RAG ``chat_id``. Binding the
        equipment dataset chat causes retrieval to contaminate observations
        with knowledge-base facts that are not in the image.
        """
        del chat_id  # kept for call-site compatibility; must not bind RAG chat
        prompt = (
            "只根据当前附件图片中肉眼可见的文字与控件作答。"
            "禁止使用知识库、设备台账、会话历史或猜测补全。"
            "若图片与设备无关，也如实描述图片内容。"
            "只输出 JSON："
            '{"errorCodes":[],"equipmentCodes":[],"visibleValues":[],'
            '"textSpans":[],"confidence":0.0}。这些是观察，不是设备台账。'
        )
        if isinstance(file, dict):
            attachment = file
        else:
            raise RAGFlowAPIError(
                "Chat attachment must be a descriptor with mime_type",
                502,
                request_id,
            )
        if not attachment.get("id") or not attachment.get("mime_type"):
            raise RAGFlowAPIError(
                "Chat attachment descriptor missing id or mime_type",
                502,
                request_id,
            )
        result = await self.chat_completion(
            None,
            prompt,
            session_id=None,
            files=[attachment],
            request_id=request_id,
        )
        data = result.get("data", {}) if isinstance(result, dict) else {}
        answer = str(data.get("answer") or "").strip()
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError:
            start = answer.find("{")
            end = answer.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(answer[start : end + 1])
                except json.JSONDecodeError:
                    parsed = {"textSpans": [answer[:500]]}
            else:
                parsed = {"textSpans": [answer[:500]]} if answer else {}
        return parsed if isinstance(parsed, dict) else {}

    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        files: list[dict[str, Any]] | list[str] | None = None,
        internet: bool = False,
        messages: list[dict[str, Any]] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        allowed_identifiers: list[str] | None = None,
        scope_identifiers: list[str] | None = None,
        attachment_observations: list[str] | None = None,
        reasoning: int | None = None,
    ):
        """Stream RAGFlow chat completion over the public SSE API.

        Yields parsed ``{"code", "message", "data"}`` payloads; the terminal
        payload has ``data=True``.
        """
        import httpx

        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "stream": True,
        }
        if messages is not None:
            body["messages"] = [dict(item) for item in messages]
            if files and body["messages"]:
                for item in reversed(body["messages"]):
                    if item.get("role") == "user":
                        item["files"] = list(files)
                        break
        else:
            body["question"] = question
        if session_id:
            body["session_id"] = session_id
        if doc_ids:
            body["doc_ids"] = ",".join(doc_ids)
        if files and messages is None:
            body["files"] = list(files)
        if internet:
            body["internet"] = True
        if store_history_messages is not None:
            body["store_history_messages"] = bool(store_history_messages)
        if pass_all_history_messages is not None:
            body["pass_all_history_messages"] = bool(pass_all_history_messages)
        if grounding_version is not None:
            body["grounding_version"] = grounding_version
        if allowed_identifiers:
            body["allowed_identifiers"] = [str(item) for item in allowed_identifiers if str(item)]
        if scope_identifiers:
            body["scope_identifiers"] = [str(item) for item in scope_identifiers if str(item)]
        if attachment_observations:
            body["attachment_observations"] = [
                str(item) for item in attachment_observations if str(item)
            ]
        if reasoning is not None:
            body["reasoning"] = int(reasoning)
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

    _AUTO_EFFECTIVE_KNOWLEDGE = object()

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
        self._omit_status = False
        self._chunks_by_id: dict[str, dict] = {}
        self.uploaded_files: list[str] = []
        self.deleted_files: list[str] = []
        self._ragflow_files: dict[str, bytes] = {}
        self._last_understand_file: dict[str, Any] | None = None
        self.understand_calls = 0
        self.default_llm_setting: dict[str, Any] = {"model_type": "chat"}
        self.forced_answer: str | None = None
        self.grounding: dict[str, Any] | None = {
            "version": 1,
            "effectiveKnowledge": self._AUTO_EFFECTIVE_KNOWLEDGE,
        }
        self.understand_result: dict[str, Any] = {
            "errorCodes": ["E07"],
            "equipmentCodes": [],
            "visibleValues": [],
            "textSpans": [],
            "confidence": 0.8,
        }

    async def get_document_image(
        self,
        image_id: str,
        request_id: str | None = None,
    ) -> tuple[bytes, str] | None:
        del image_id, request_id
        return None

    async def start_parsing(
        self,
        dataset_id: str,
        document_ids: list[str],
        request_id: str | None = None,
    ) -> dict:
        return {"code": 0, "data": True}

    async def get_chunk_evidence(
        self,
        dataset_id: str,
        document_id: str,
        chunk_id: str,
        request_id: str | None = None,
    ) -> dict:
        chunk = self._chunks_by_id.get(chunk_id)
        if not chunk or chunk.get("document_id") != document_id:
            return {}
        return dict(chunk)

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
        prompt_config: dict | None = None,
    ) -> dict:
        chat = {
            "id": f"chat-{uuid.uuid4().hex[:12]}",
            "name": name,
            "dataset_ids": list(dataset_ids),
            "llm_setting": dict(self.default_llm_setting),
        }
        if prompt_config is not None:
            chat["prompt_config"] = dict(prompt_config)
        self._chats[chat["id"]] = chat
        return {"code": 0, "data": chat}

    async def create_session(
        self,
        chat_id: str,
        name: str,
        request_id: str | None = None,
    ) -> dict:
        del request_id
        if chat_id not in self._chats:
            raise RAGFlowAPIError("Stub: chat not found", 404)
        session = {
            "id": f"session-{uuid.uuid4().hex[:12]}",
            "chat_id": chat_id,
            "name": name,
            "messages": [],
            "reference": [],
        }
        self._sessions[session["id"]] = session
        return {"code": 0, "data": dict(session)}

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
        prompt_config: dict | None = None,
    ) -> dict:
        chat = self._chats.get(chat_id)
        if chat is None:
            raise RAGFlowAPIError("Stub: chat not found", 404)
        if dataset_ids is not None:
            chat["dataset_ids"] = list(dataset_ids)
        if prompt_config is not None:
            chat["prompt_config"] = dict(prompt_config)
        return {"code": 0, "data": chat}

    async def delete_dataset(
        self,
        dataset_id: str,
        request_id: str | None = None,
    ) -> dict:
        self._datasets.pop(dataset_id, None)
        return {"code": 0, "data": True}

    async def upload_chat_file(
        self,
        file_name: str,
        content: bytes,
        media_type: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self.uploaded_files.append(file_name)
        file_id = f"rf-{len(self.uploaded_files)}"
        self._ragflow_files[file_id] = content
        return {
            "id": file_id,
            "name": file_name,
            "mime_type": media_type,
            "created_by": "stub-tenant",
        }

    async def delete_file(
        self,
        file_id: str,
        request_id: str | None = None,
        created_by: str | None = None,
    ) -> None:
        del request_id, created_by
        self.deleted_files.append(file_id)
        self._ragflow_files.pop(file_id, None)

    async def understand_file(
        self,
        chat_id: str | None,
        file: dict[str, Any] | str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        del chat_id, request_id
        if isinstance(file, str):
            raise RAGFlowAPIError(
                "Chat attachment must be a descriptor with mime_type", 502
            )
        self.understand_calls += 1
        self._last_understand_file = dict(file)
        return dict(self.understand_result)

    def _terminal_status(self, answer: str | None) -> str | None:
        """Explicit ``data.status`` mirroring RF-PATCH-007 in dialog_service.

        Exact-match only: abstain when the final answer is blank or exactly the
        standard abstain text. ``_omit_status`` drops the field entirely so
        Gateway contract tests can exercise the missing-status path.
        """
        if self._omit_status:
            return None
        text = str(answer or "").strip()
        if not text or text == _STANDARD_ABSTAIN_ANSWER:
            return "no_reliable_evidence"
        return "completed"

    def _apply_terminal_status(self, data: dict, answer: str | None) -> dict:
        status = self._terminal_status(answer)
        if status is not None:
            data["status"] = status
        return data

    async def chat_completion(
        self,
        chat_id: str | None,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        files: list[dict[str, Any]] | list[str] | None = None,
        internet: bool = False,
        messages: list[dict[str, Any]] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        allowed_identifiers: list[str] | None = None,
        scope_identifiers: list[str] | None = None,
        attachment_observations: list[str] | None = None,
        reasoning: int | None = None,
    ) -> dict:
        turn_id = f"msg-{uuid.uuid4().hex[:12]}"
        body_messages = [dict(item) for item in messages] if messages is not None else None
        if files and body_messages:
            for item in reversed(body_messages):
                if item.get("role") == "user":
                    item["files"] = list(files)
                    break
        self._last_completion_body = {
            "chat_id": chat_id,
            "question": question,
            "session_id": session_id,
            "doc_ids": ",".join(doc_ids) if doc_ids else None,
            "files": list(files) if files else [],
            "internet": internet,
            "messages": body_messages,
            "store_history_messages": store_history_messages,
            "pass_all_history_messages": pass_all_history_messages,
            "grounding_version": grounding_version,
            "allowed_identifiers": list(allowed_identifiers or []),
            "scope_identifiers": list(scope_identifiers or []),
            "attachment_observations": list(attachment_observations or []),
        }
        if reasoning is not None:
            self._last_completion_body["reasoning"] = int(reasoning)
        base_chunk = {
            "id": "chunk-1",
            "content": "故障码 E-104 时先检查液压油位。",
            "document_id": "doc-1",
            "document_name": "manual.pdf",
            "positions": [[3, 0.1, 0.8, 0.2, 0.4]],
        }
        if self._no_evidence:
            session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
            self._append_no_evidence_turn(
                session_id, question, turn_id
            )
            data = self._apply_terminal_status(
                {
                    "answer": "",
                    "id": turn_id,
                    "reference": {"chunks": []},
                },
                "",
            )
            if session_id:
                data["session_id"] = session_id
            return {
                "code": 0,
                "data": data,
            }
        if self._empty_answer:
            session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
            self._append_no_evidence_turn(
                session_id, question, turn_id, chunks=[base_chunk]
            )
            data = self._apply_terminal_status(
                {
                    "answer": "",
                    "id": turn_id,
                    "reference": {"chunks": [base_chunk]},
                },
                "",
            )
            if session_id:
                data["session_id"] = session_id
            return {
                "code": 0,
                "data": data,
            }
        if self._empty_chunks:
            session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
            self._append_no_evidence_turn(
                session_id, question, turn_id
            )
            data = self._apply_terminal_status(
                {
                    "answer": "stub answer",
                    "id": turn_id,
                    "reference": {"chunks": []},
                },
                "stub answer",
            )
            if session_id:
                data["session_id"] = session_id
            return {
                "code": 0,
                "data": data,
            }
        session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
        session = (
            self._sessions.setdefault(
                session_id, {"messages": [], "reference": []}
            )
            if session_id
            else None
        )
        chunks = [
            base_chunk
        ] + list(self._extra_chunks)
        if self._omit_default_chunk:
            chunks = list(self._extra_chunks)
        if doc_ids and not self._ignore_doc_scope:
            allowed = set(doc_ids)
            chunks = [c for c in chunks if c.get("document_id") in allowed]
        self._chunks_by_id.update(
            {
                str(chunk.get("id") or chunk.get("chunk_id")): dict(chunk)
                for chunk in chunks
                if chunk.get("id") or chunk.get("chunk_id")
            }
        )
        if self.forced_answer is not None:
            answer = self.forced_answer
        else:
            markers = "".join(f" [ID:{index}]" for index in range(len(chunks)))
            answer = f"stub answer{markers}"
        if session is not None:
            session["messages"].append(
                {"role": "user", "content": question, "id": turn_id}
            )
            session["messages"].append(
                {
                    "role": "assistant",
                    "content": answer,
                    "id": turn_id,
                }
            )
            session["reference"].append({"chunks": chunks})
        data = self._apply_terminal_status(
            {
                "answer": answer,
                "id": turn_id,
                "reference": {"chunks": chunks},
            },
            answer,
        )
        if session_id:
            data["session_id"] = session_id
        return {
            "code": 0,
            "data": data,
        }

    async def chat_completion_stream(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        doc_ids: list[str] | None = None,
        request_id: str | None = None,
        files: list[dict[str, Any]] | list[str] | None = None,
        internet: bool = False,
        messages: list[dict[str, Any]] | None = None,
        store_history_messages: bool | None = None,
        pass_all_history_messages: bool | None = None,
        grounding_version: int | None = None,
        allowed_identifiers: list[str] | None = None,
        scope_identifiers: list[str] | None = None,
        attachment_observations: list[str] | None = None,
        reasoning: int | None = None,
    ):
        if self._stream_fail_after == 0:
            completion = await self.chat_completion(
                chat_id,
                question,
                session_id=session_id,
                doc_ids=doc_ids,
                request_id=request_id,
                files=files,
                internet=internet,
                messages=messages,
                store_history_messages=store_history_messages,
                pass_all_history_messages=pass_all_history_messages,
                grounding_version=grounding_version,
                allowed_identifiers=allowed_identifiers,
                scope_identifiers=scope_identifiers,
                attachment_observations=attachment_observations,
                reasoning=reasoning,
            )
            data = completion.get("data", {})
            stream_id = None if self._omit_stream_id else (
                data.get("id") if isinstance(data, dict) else None
            )
            session = data.get("session_id") if isinstance(data, dict) else None
            reference = data.get("reference", {}) if isinstance(data, dict) else {}
            answer = data.get("answer", "") if isinstance(data, dict) else ""
            status = (
                data.get("status")
                if isinstance(data, dict) and isinstance(data.get("status"), str)
                else None
            )
            for piece in _chunk_stream_text(str(answer or "")):
                await asyncio.sleep(self._stream_delay)
                yield {
                    "code": 0,
                    "message": "",
                    "data": {
                        "answer": piece,
                        "id": stream_id,
                        "session_id": session,
                    },
                }
            final_data: dict[str, Any] = {
                "answer": "",
                "id": stream_id,
                "session_id": session,
                "reference": reference,
                "final": True,
            }
            if status is not None:
                final_data["status"] = status
            yield {
                "code": 0,
                "message": "",
                "data": final_data,
            }
            yield {"code": 0, "message": "", "data": True}
            return
        self._stream_fail_after -= 1
        raise RAGFlowAPIError("Stub: stream failed mid-flight", 503)

    def _append_no_evidence_turn(
        self,
        session_id: str | None,
        question: str,
        turn_id: str,
        *,
        chunks: list[dict] | None = None,
    ) -> None:
        if not session_id:
            return
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
