"""Gateway orchestration for EAM user long-term Memory (phase 1).

Flow: Search (before Chat) → Chat → async Memory Candidate after full success.
Write failure must not block answers; read failure/timeout → empty memory continue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from enterprise.gateway.config import config
from enterprise.gateway.query.memory_client import RAGFlowMemoryClient
from enterprise.gateway.query.memory_subject import memory_subject_from_principal

logger = logging.getLogger(__name__)

_pending_tasks: set[asyncio.Task] = set()
_warned_missing_config = False
_memory_client: RAGFlowMemoryClient | None = None


def _user_memory_runtime():
    """Effective hot-reloadable snapshot (not frozen boot-only config)."""
    return config.runtime_settings()


def user_memory_enabled() -> bool:
    """Feature gate from runtime snapshot (env/Console; default false)."""
    return bool(_user_memory_runtime().user_memory_enabled)


def enterprise_memory_id() -> str:
    return str(_user_memory_runtime().user_memory_id or "").strip()


def user_memory_top_n() -> int:
    return int(_user_memory_runtime().user_memory_top_n)


def user_memory_timeout_seconds() -> float:
    return float(_user_memory_runtime().user_memory_timeout_seconds)


def memory_config_ready() -> bool:
    return user_memory_enabled() and bool(enterprise_memory_id())


def _warn_missing_config_once() -> None:
    global _warned_missing_config
    if _warned_missing_config:
        return
    _warned_missing_config = True
    if user_memory_enabled() and not enterprise_memory_id():
        logger.warning(
            "user_memory_noop reason=missing_memoryId "
            "(enabled=true but memoryId empty; Q&A continues without Memory)"
        )
    elif not user_memory_enabled():
        logger.info(
            "user_memory_disabled enabled=false "
            "(default; Q&A unaffected)"
        )


def get_memory_client() -> RAGFlowMemoryClient:
    global _memory_client
    if _memory_client is None:
        _memory_client = RAGFlowMemoryClient()
    return _memory_client


def set_memory_client_for_tests(client: RAGFlowMemoryClient | None) -> None:
    global _memory_client
    _memory_client = client


def format_memory_hits(hits: list[dict[str, Any]], *, top_n: int | None = None) -> str:
    """Format TopK Memory hits as weak-context text for {user_memory}."""
    limit = int(top_n if top_n is not None else user_memory_top_n())
    lines: list[str] = []
    for idx, hit in enumerate(hits[: max(0, limit)], start=1):
        content = str(hit.get("content") or "").strip()
        if not content:
            user_input = str(hit.get("user_input") or "").strip()
            agent_response = str(hit.get("agent_response") or "").strip()
            if user_input or agent_response:
                content = f"User Input: {user_input}\nAgent Response: {agent_response}".strip()
        if not content:
            continue
        lines.append(f"{idx}. {content}")
    return "\n".join(lines)


async def fetch_user_memory_text(
    principal: Any,
    query: str,
    *,
    request_id: str | None = None,
) -> str:
    """Search enterprise Memory pool before Chat. Failures → empty string."""
    _warn_missing_config_once()
    if not memory_config_ready():
        return ""
    try:
        subject = memory_subject_from_principal(principal)
    except ValueError as exc:
        logger.warning("user_memory_search_skipped reason=bad_subject error=%s", exc)
        return ""
    question = str(query or "").strip()
    if not question:
        return ""
    try:
        client = get_memory_client()
        client.timeout = user_memory_timeout_seconds()
        top_n = user_memory_top_n()
        hits = await client.search_messages(
            memory_id=enterprise_memory_id(),
            query=question,
            user_id=subject,
            top_n=top_n,
            request_id=request_id,
        )
        return format_memory_hits(hits, top_n=top_n)
    except Exception as exc:
        logger.warning(
            "user_memory_search_failed request_id=%s error_type=%s error=%s",
            request_id,
            type(exc).__name__,
            exc,
        )
        return ""


def memory_agent_id(chat_id: str | None) -> str:
    cid = str(chat_id or "").strip()
    return f"chat:{cid}" if cid else "chat:unknown"


async def _add_memory_candidate(
    *,
    subject: str,
    chat_id: str | None,
    session_id: str | None,
    user_input: str,
    agent_response: str,
    request_id: str | None,
) -> None:
    try:
        client = get_memory_client()
        client.timeout = user_memory_timeout_seconds()
        await client.add_message(
            memory_id=enterprise_memory_id(),
            agent_id=memory_agent_id(chat_id),
            session_id=str(session_id or "").strip() or "unknown",
            user_id=subject,
            user_input=user_input,
            agent_response=agent_response,
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning(
            "user_memory_add_failed request_id=%s error_type=%s error=%s",
            request_id,
            type(exc).__name__,
            exc,
        )


def schedule_memory_candidate(
    principal: Any,
    *,
    chat_id: str | None,
    session_id: str | None,
    user_input: str,
    agent_response: str,
    request_id: str | None = None,
) -> None:
    """Fire-and-forget Candidate write after a successful full answer."""
    _warn_missing_config_once()
    if not memory_config_ready():
        return
    answer = str(agent_response or "").strip()
    question = str(user_input or "").strip()
    if not question or not answer:
        return
    try:
        subject = memory_subject_from_principal(principal)
    except ValueError as exc:
        logger.warning("user_memory_add_skipped reason=bad_subject error=%s", exc)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "user_memory_add_skipped reason=no_running_loop request_id=%s",
            request_id,
        )
        return

    task = loop.create_task(
        _add_memory_candidate(
            subject=subject,
            chat_id=chat_id,
            session_id=session_id,
            user_input=question,
            agent_response=answer,
            request_id=request_id,
        ),
        name=f"user-memory-add-{request_id or 'anon'}",
    )
    _pending_tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        _pending_tasks.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.warning(
                "user_memory_add_task_failed request_id=%s error=%s",
                request_id,
                exc,
            )

    task.add_done_callback(_done)