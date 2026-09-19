"""RAGFlow Memory HTTP client (official /api/v1/messages*).

Uses the same Bearer RAGFLOW_API_KEY as other Gateway RAGFlow clients.
Failures are soft for Gateway Q&A orchestration — callers decide whether to
continue with empty memory or only log write failures.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from enterprise.gateway.config import config, require_ragflow_api_key
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowDocumentClient,
)

logger = logging.getLogger(__name__)


class RAGFlowMemoryClient(RAGFlowDocumentClient):
    """Thin wrapper around RF Memory search / add / list / forget APIs."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or config.ragflow_base_url,
            api_key=api_key or require_ragflow_api_key(),
        )
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            try:
                self.timeout = float(config.runtime_settings().user_memory_timeout_seconds)
            except Exception:
                self.timeout = float(getattr(config, "user_memory_timeout", 5.0))

    def _require_ok(self, result: dict) -> dict:
        if not isinstance(result, dict):
            raise RuntimeError("RAGFlow Memory returned a non-object payload")
        code = result.get("code")
        if code not in (0, None):
            raise RuntimeError(
                str(result.get("message") or f"RAGFlow Memory error code={code}")
            )
        return result

    async def search_messages(
        self,
        *,
        memory_id: str,
        query: str,
        user_id: str,
        top_n: int = 5,
        similarity_threshold: float = 0.2,
        keywords_similarity_weight: float = 0.7,
        agent_id: str = "",
        session_id: str = "",
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rid = request_id or self._new_request_id()
        params: dict[str, Any] = {
            "memory_id": memory_id,
            "query": query,
            "user_id": user_id,
            "top_n": int(top_n),
            "similarity_threshold": float(similarity_threshold),
            "keywords_similarity_weight": float(keywords_similarity_weight),
        }
        if agent_id:
            params["agent_id"] = agent_id
        if session_id:
            params["session_id"] = session_id
        path = f"/api/v1/messages/search?{urlencode(params, doseq=True)}"
        result = await self._run_sync(
            self._sync_request,
            "GET",
            path,
            rid,
            timeout=self.timeout,
        )
        data = self._require_ok(result).get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            messages = data.get("messages") or data.get("chunks") or []
            if isinstance(messages, list):
                return [item for item in messages if isinstance(item, dict)]
        return []

    async def add_message(
        self,
        *,
        memory_id: str,
        agent_id: str,
        session_id: str,
        user_id: str,
        user_input: str,
        agent_response: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        rid = request_id or self._new_request_id()
        body = {
            "memory_id": [memory_id] if isinstance(memory_id, str) else list(memory_id),
            "agent_id": agent_id,
            "session_id": session_id,
            "user_id": user_id,
            "user_input": user_input,
            "agent_response": agent_response,
        }
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/messages",
            rid,
            json_data=body,
            timeout=self.timeout,
        )
        return self._require_ok(result)

    async def list_messages(
        self,
        *,
        memory_id: str,
        user_id: str = "",
        agent_id: str = "",
        session_id: str = "",
        limit: int = 20,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent messages; Gateway forces subject filter client-side."""
        rid = request_id or self._new_request_id()
        params: dict[str, Any] = {
            "memory_id": memory_id,
            "limit": int(limit),
        }
        if agent_id:
            params["agent_id"] = agent_id
        if session_id:
            params["session_id"] = session_id
        path = f"/api/v1/messages?{urlencode(params, doseq=True)}"
        result = await self._run_sync(
            self._sync_request,
            "GET",
            path,
            rid,
            timeout=self.timeout,
        )
        data = self._require_ok(result).get("data")
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            messages = data.get("messages") or []
            if isinstance(messages, list):
                items = [item for item in messages if isinstance(item, dict)]
        if user_id:
            items = [
                item for item in items if str(item.get("user_id") or "") == user_id
            ]
        return items

    async def forget_message(
        self,
        *,
        memory_id: str,
        message_id: int | str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        rid = request_id or self._new_request_id()
        path = f"/api/v1/messages/{memory_id}:{message_id}"
        result = await self._run_sync(
            self._sync_request,
            "DELETE",
            path,
            rid,
            timeout=self.timeout,
        )
        return self._require_ok(result)


class RAGFlowMemoryStub(RAGFlowMemoryClient):
    """In-memory stub for unit tests."""

    def __init__(self) -> None:
        super().__init__(base_url="stub://memory", api_key="stub-key", timeout=1.0)
        self._messages: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []

    async def search_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(dict(kwargs))
        user_id = str(kwargs.get("user_id") or "")
        return [
            item
            for item in self._messages
            if not user_id or str(item.get("user_id") or "") == user_id
        ][: int(kwargs.get("top_n") or 5)]

    async def add_message(self, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append(dict(kwargs))
        msg = {
            "message_id": len(self._messages) + 1,
            "memory_id": kwargs.get("memory_id"),
            "user_id": kwargs.get("user_id"),
            "agent_id": kwargs.get("agent_id"),
            "session_id": kwargs.get("session_id"),
            "content": (
                f"User Input: {kwargs.get('user_input')}\n"
                f"Agent Response: {kwargs.get('agent_response')}"
            ),
            "status": 1,
        }
        self._messages.append(msg)
        return {"code": 0, "message": "ok", "data": True}

    async def list_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        user_id = str(kwargs.get("user_id") or "")
        items = list(self._messages)
        if user_id:
            items = [item for item in items if str(item.get("user_id") or "") == user_id]
        return items[: int(kwargs.get("limit") or 20)]

    async def forget_message(self, **kwargs: Any) -> dict[str, Any]:
        mid = int(kwargs["message_id"])
        self._messages = [
            item for item in self._messages if int(item.get("message_id") or 0) != mid
        ]
        return {"code": 0, "message": "ok", "data": True}