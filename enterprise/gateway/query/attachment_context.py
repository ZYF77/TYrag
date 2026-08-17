"""Conversation attachment observations used only to enrich retrieval."""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MESSAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "text/plain", "application/pdf"}
)
MAX_MESSAGE_FILES = 5
_ERROR_CODE_RE = re.compile(r"\b[A-Z]?E-?\d{2,4}\b")


@dataclass
class PendingAttachment:
    file_name: str
    media_type: str
    content: bytes
    sha256: str
    size_bytes: int
    attachment_id: str | None = None


@dataclass
class AttachmentObservation:
    trust_level: str = "observed"
    text_spans: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    equipment_codes: list[str] = field(default_factory=list)
    visible_values: list[str] = field(default_factory=list)
    confidence: float | None = None
    understood: bool = False


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        pages = PdfReader(io.BytesIO(content)).pages
        return "\n".join((page.extract_text() or "") for page in pages).strip()
    except Exception:
        return ""


def _from_raw(raw: dict[str, Any] | None) -> AttachmentObservation:
    data = raw if isinstance(raw, dict) else {}
    error_codes = [str(item).strip() for item in data.get("errorCodes") or [] if str(item).strip()]
    text_spans = [str(item).strip() for item in data.get("textSpans") or [] if str(item).strip()]
    equipment_codes = [
        str(item).strip() for item in data.get("equipmentCodes") or [] if str(item).strip()
    ]
    visible_values = [
        str(item).strip() for item in data.get("visibleValues") or [] if str(item).strip()
    ]
    understood = bool(error_codes or text_spans or equipment_codes or visible_values)
    confidence = data.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_value = None
    return AttachmentObservation(
        trust_level="observed",
        text_spans=text_spans[:8],
        error_codes=error_codes[:8],
        equipment_codes=equipment_codes[:8],
        visible_values=visible_values[:8],
        confidence=confidence_value,
        understood=understood,
    )


def _from_text(text: str) -> AttachmentObservation:
    cleaned = " ".join(text.split())
    if not cleaned:
        return AttachmentObservation(trust_level="observed", understood=False)
    codes = list(dict.fromkeys(_ERROR_CODE_RE.findall(cleaned)))
    return AttachmentObservation(
        trust_level="observed",
        text_spans=[cleaned[:500]],
        error_codes=codes[:8],
        understood=True,
    )


async def observe_attachments(
    pending: list[PendingAttachment],
    client: Any,
    chat_id: str | None,
    service: Any | None = None,
) -> list[AttachmentObservation]:
    observations: list[AttachmentObservation] = []
    for item in pending:
        if item.media_type == "text/plain":
            observations.append(_from_text(item.content.decode("utf-8", "replace")))
            continue
        if item.media_type == "application/pdf":
            observations.append(_from_text(_pdf_text(item.content)))
            continue
        if item.media_type not in {"image/jpeg", "image/png"} or not chat_id:
            observations.append(AttachmentObservation(trust_level="observed"))
            continue
        file_id = await client.upload_chat_file(
            item.file_name, item.content, item.media_type
        )
        if service is not None and item.attachment_id and hasattr(service, "set_ragflow_file"):
            await service.set_ragflow_file(item.attachment_id, file_id)
        try:
            raw = await client.understand_file(chat_id, file_id)
            observations.append(_from_raw(raw))
        except Exception:
            logger.warning("attachment understand failed name=%s", item.file_name)
            observations.append(AttachmentObservation(trust_level="observed"))
        finally:
            try:
                await client.delete_file(file_id)
                if service is not None and item.attachment_id and hasattr(
                    service, "mark_ragflow_file_deleted"
                ):
                    await service.mark_ragflow_file_deleted(item.attachment_id)
            except Exception:
                logger.warning("RAGFlow temp file delete failed file_id=%s", file_id)
    return observations


def any_understood(observations: list[AttachmentObservation]) -> bool:
    return any(item.understood for item in observations)


def enrich_question(original: str, observations: list[AttachmentObservation]) -> str:
    facts: list[str] = []
    for item in observations:
        facts.extend(item.error_codes)
        facts.extend(item.equipment_codes)
        facts.extend(item.visible_values)
        facts.extend(item.text_spans[:2])
    facts = [item for item in dict.fromkeys(facts) if item]
    if not facts:
        return original
    joined = "、".join(facts[:12])
    prefix = (
        f"【上传附件观察，非设备台账】识别到疑似：{joined}。"
        "回答时必须写成「从你上传的图片中识别到疑似故障码 …」，"
        "禁止写成「设备当前故障码是 …」。\n"
    )
    if original.strip():
        return prefix + "用户问题：" + original
    return prefix + "请根据知识库解释这些观察。"
