"""Rolling conversation summary for long v2 inquiry sessions."""
from __future__ import annotations

import logging

from enterprise.gateway.config import config
from enterprise.gateway.query import v2_store

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT_PREFIX = (
    "请把下列对话整理为简洁的中文滚动摘要，保留设备、故障、已确认结论与待办。"
    "不要编造未出现的事实。只输出摘要正文。\n\n"
)


def rag_question_with_summary(conversation: dict, question: str) -> str:
    summary = (conversation.get("context_summary") or "").strip()
    if not summary:
        return question
    return f"[先前对话摘要]\n{summary}\n\n[当前问题]\n{question}"


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


async def maybe_compress_conversation(
    db,
    *,
    conversation: dict,
    principal_tenant_id: str,
    principal_business_user_id: str,
    client,
    chat_id: str | None,
) -> dict:
    """Compress older turns after a successful ask; never raise to callers."""
    if not config.context_compress_enabled:
        return conversation
    conversation_id = conversation["conversation_id"]
    try:
        threshold = max(2, int(config.context_compress_turns))
        keep_recent = max(0, int(config.context_compress_keep_recent))
        max_chars = max(200, int(config.context_summary_max_chars))
        messages = await v2_store.list_messages_ordered(
            db,
            conversation_id=conversation_id,
            tenant_id=principal_tenant_id,
            business_user_id=principal_business_user_id,
        )
        total = len(messages)
        if total < threshold:
            return conversation
        watermark = int(conversation.get("compressed_turn_watermark") or 0)
        end = total - keep_recent
        if end <= watermark:
            return conversation
        window = messages[watermark:end]
        if not window:
            return conversation
        transcript_lines = []
        for item in window:
            role = "用户" if item.get("role") == "user" else "助手"
            content = str(item.get("content") or "").strip()
            if content:
                transcript_lines.append(f"{role}: {content}")
        if not transcript_lines:
            return conversation
        prior = (conversation.get("context_summary") or "").strip()
        prompt_parts = [_SUMMARY_PROMPT_PREFIX]
        if prior:
            prompt_parts.append(f"[已有摘要]\n{prior}\n\n")
        prompt_parts.append("[新增对话]\n")
        prompt_parts.append("\n".join(transcript_lines))
        summary_question = "".join(prompt_parts)
        if not chat_id or client is None:
            # Deterministic fallback for empty-scope / offline paths.
            summary = _truncate(
                (prior + "\n" if prior else "")
                + "\n".join(transcript_lines),
                max_chars,
            )
        else:
            completion = await client.chat_completion(
                chat_id,
                summary_question,
                session_id=None,
                doc_ids=None,
            )
            data = completion.get("data", {}) if isinstance(completion, dict) else {}
            summary = _truncate(str(data.get("answer") or ""), max_chars)
            if not summary:
                summary = _truncate(
                    (prior + "\n" if prior else "")
                    + "\n".join(transcript_lines),
                    max_chars,
                )
        updated = await v2_store.save_context_summary(
            db,
            conversation_id=conversation_id,
            tenant_id=principal_tenant_id,
            business_user_id=principal_business_user_id,
            context_summary=summary,
            compressed_turn_watermark=end,
            clear_ragflow_session=True,
        )
        return updated or conversation
    except Exception:
        logger.exception(
            "context compress failed conversation_id=%s", conversation_id
        )
        return conversation
