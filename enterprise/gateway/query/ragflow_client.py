"""RAGFlow client additions used by the query demo router."""
from typing import Any
import uuid

from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
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
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chat/completions",
            rid,
            json_data=body,
        )
        return self._require_ok(result)


class RAGFlowQueryStub(RAGFlowDocumentStub):
    """Offline stub for the query demo loop."""

    def __init__(self) -> None:
        super().__init__()
        self._chats: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}

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

    async def chat_completion(
        self,
        chat_id: str,
        question: str,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        session_id = session_id or "stub-session"
        session = self._sessions.setdefault(
            session_id, {"messages": [], "reference": []}
        )
        session["messages"].append(
            {"role": "user", "content": question}
        )
        session["messages"].append(
            {
                "role": "assistant",
                "content": f"stub answer for: {question}",
            }
        )
        session["reference"].append(
            {
                "chunks": [
                    {
                        "id": "chunk-1",
                        "content": "故障码 E-104 时先检查液压油位。",
                        "document_id": "doc-1",
                        "document_name": "manual.pdf",
                        "positions": [[3, 0.1, 0.2, 0.8, 0.4]],
                    }
                ]
            }
        )
        return {
            "code": 0,
            "data": {
                "answer": f"stub answer for: {question}",
                "session_id": session_id,
                "reference": {"chunks": session["reference"][-1]["chunks"]},
            },
        }

    async def get_session(
        self,
        chat_id: str,
        session_id: str,
        request_id: str | None = None,
    ) -> dict:
        return {
            "code": 0,
            "data": self._sessions.get(
                session_id, {"messages": [], "reference": []}
            ),
        }
