"""Conversation attachment observations used to enrich retrieval.

Original files are uploaded once and may also be passed to the final
RAGFlow completion via ``files[]``. Gateway only extracts cheap local
observations (image Understand, TXT/PDF text-layer); it does not parse
Office bodies.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field
from email.header import decode_header
from typing import Any

logger = logging.getLogger(__name__)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
OFFICE_MEDIA_TYPES = frozenset({DOCX_MEDIA_TYPE, XLSX_MEDIA_TYPE})
MESSAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "text/plain", "application/pdf"}
    | OFFICE_MEDIA_TYPES
)
_MIME_SUFFIXES = {
    "text/plain": (".txt",),
    "application/pdf": (".pdf",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    DOCX_MEDIA_TYPE: (".docx",),
    XLSX_MEDIA_TYPE: (".xlsx",),
}
MAX_MESSAGE_FILES = 5
_ERROR_CODE_RE = re.compile(r"\b[A-Z]?E-?\d{2,4}\b")
_VISION_MODEL_TYPES = frozenset({"chat", "vision", "image2text", "img2txt"})
_PASS_IMAGES_OFF = frozenset({"0", "false", "no"})


def decode_content_disposition_filename(raw: str | None) -> str:
    """Decode RFC 2047 encoded-words used by some EAM multipart clients."""
    text = str(raw or "").strip() or "attachment"
    if "=?" in text:
        try:
            pieces: list[str] = []
            for chunk, charset in decode_header(text):
                if isinstance(chunk, bytes):
                    pieces.append(chunk.decode(charset or "utf-8", "replace"))
                else:
                    pieces.append(str(chunk))
            text = "".join(pieces).strip() or "attachment"
        except Exception:
            pass
    name = os.path.basename(text.replace("\\", "/")).strip()
    return name or "attachment"


def ensure_media_suffix(name: str, media_type: str) -> str:
    """Give RAGFlow naive.chunk a filename extension it can recognize."""
    suffixes = _MIME_SUFFIXES.get(media_type)
    if not suffixes:
        return name
    lowered = name.lower()
    if any(lowered.endswith(item) for item in suffixes):
        return name
    return name + suffixes[0]


def ragflow_attachment_filename(raw: str | None, media_type: str) -> str:
    return ensure_media_suffix(decode_content_disposition_filename(raw), media_type)


@dataclass
class PendingAttachment:
    file_name: str
    media_type: str
    content: bytes
    sha256: str
    size_bytes: int
    attachment_id: str | None = None
    ragflow_file: dict | None = None


@dataclass
class AttachmentObservation:
    trust_level: str = "observed"
    text_spans: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    equipment_codes: list[str] = field(default_factory=list)
    visible_values: list[str] = field(default_factory=list)
    confidence: float | None = None
    understood: bool = False


def chat_is_vision_capable(chat: dict | None) -> bool:
    """True when original images should be passed in RAGFlow ``files[]``.

    Aligns with RAGFlow ``convert_last_user_msg_to_multimodal`` (``model_type ==
    "chat"``). ``ENTERPRISE_CHAT_PASS_IMAGES=0|false|no`` is an emergency off
    switch; images then stay enrichment-only. ``llm_id`` is ignored.
    """
    raw = os.environ.get("ENTERPRISE_CHAT_PASS_IMAGES", "")
    if str(raw).strip().lower() in _PASS_IMAGES_OFF:
        return False
    setting = (chat or {}).get("llm_setting") or {}
    model_type = setting.get("model_type")
    if isinstance(model_type, list):
        if not model_type:
            return True
        return any(str(item).lower() in _VISION_MODEL_TYPES for item in model_type)
    if isinstance(model_type, str):
        normalized = model_type.strip().lower()
        if not normalized:
            return True
        return normalized in _VISION_MODEL_TYPES
    return model_type is None


def completion_files(
    pending: list[PendingAttachment], *, vision: bool
) -> list[dict]:
    """Descriptors for the final chat_completion / stream ``files[]``.

    Images are included only when the Chat model is vision-capable.
    docx/xlsx/txt/pdf still go through for text-side parse.
    """
    files: list[dict] = []
    for item in pending:
        if not item.ragflow_file:
            continue
        if item.media_type in IMAGE_MEDIA_TYPES and not vision:
            continue
        files.append(item.ragflow_file)
    return files


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


async def _upload_pending(
    item: PendingAttachment, client: Any, db: Any | None
) -> dict | None:
    from enterprise.gateway.sync.transient_attachment import remember_ragflow_temp_file

    try:
        file_desc = await client.upload_chat_file(
            ragflow_attachment_filename(item.file_name, item.media_type),
            item.content,
            item.media_type,
        )
    except Exception as exc:
        logger.warning(
            "attachment upload failed name=%s err=%s",
            item.file_name,
            type(exc).__name__,
        )
        return None
    if not isinstance(file_desc, dict):
        return None
    file_id = str(file_desc.get("id") or "")
    if not file_id:
        return None
    item.ragflow_file = file_desc
    if db is not None:
        try:
            await remember_ragflow_temp_file(db, file_id)
        except Exception:
            logger.warning("RAGFlow temp file ledger write failed file_id=%s", file_id)
    return file_desc


async def observe_attachments(
    pending: list[PendingAttachment],
    client: Any,
    chat_id: str | None,
    db: Any | None = None,
) -> list[AttachmentObservation]:
    del chat_id  # image understand uses vision-only completion, not RAG chat

    observations: list[AttachmentObservation] = []
    for item in pending:
        file_desc = await _upload_pending(item, client, db)
        if item.media_type == "text/plain":
            observations.append(_from_text(item.content.decode("utf-8", "replace")))
            continue
        if item.media_type == "application/pdf":
            observations.append(_from_text(_pdf_text(item.content)))
            continue
        if item.media_type in OFFICE_MEDIA_TYPES:
            observations.append(
                AttachmentObservation(trust_level="observed", understood=bool(file_desc))
            )
            continue
        if item.media_type not in IMAGE_MEDIA_TYPES:
            observations.append(AttachmentObservation(trust_level="observed"))
            continue
        if not file_desc:
            observations.append(AttachmentObservation(trust_level="observed"))
            continue
        try:
            raw = await client.understand_file(None, file_desc)
            observations.append(_from_raw(raw))
        except Exception as exc:
            logger.warning(
                "attachment understand failed name=%s err=%s",
                item.file_name,
                type(exc).__name__,
            )
            observations.append(AttachmentObservation(trust_level="observed"))
    return observations


async def cleanup_ragflow_files(
    pending: list[PendingAttachment],
    client: Any | None,
    db: Any | None,
) -> None:
    """Delete uploaded originals after generation (success, failure, or timeout)."""
    from enterprise.gateway.sync.transient_attachment import (
        mark_ragflow_temp_file_deleted,
    )

    if not pending or client is None:
        return
    for item in pending:
        file_id = str((item.ragflow_file or {}).get("id") or "")
        if not file_id:
            continue
        try:
            await client.delete_file(
                file_id, created_by=str((item.ragflow_file or {}).get("created_by") or "") or None
            )
        except Exception:
            logger.warning("RAGFlow temp file delete failed file_id=%s", file_id)
            continue
        if db is not None:
            try:
                await mark_ragflow_temp_file_deleted(db, file_id)
            except Exception:
                logger.warning(
                    "RAGFlow temp file ledger mark failed file_id=%s", file_id
                )


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
        "回答时必须写成「从你上传的附件中识别到疑似…」，"
        "禁止写成「设备当前故障码是 …」或把知识库内容说成图片内容。\n"
    )
    if original.strip():
        return prefix + "用户问题：" + original
    return prefix + "请根据知识库解释这些观察。"
