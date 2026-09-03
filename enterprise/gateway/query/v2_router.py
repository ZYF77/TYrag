"""Frozen v2 conversation API for the Equipment Management System."""
from __future__ import annotations

import hashlib
import json
import asyncio
import logging
import re
import uuid
from time import perf_counter
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from enterprise.gateway.acl.schema import AclScope
from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import config
from enterprise.gateway.query import v2_store
from enterprise.gateway.query.attachment_context import (
    AttachmentObservation,
    MESSAGE_MEDIA_TYPES,
    MAX_MESSAGE_FILES,
    PendingAttachment,
    chat_is_vision_capable,
    cleanup_ragflow_files,
    completion_files,
    enrich_question,
    observe_attachments,
    ragflow_attachment_filename,
)
from enterprise.gateway.query.citation_file import (
    CitationFileError,
    claim_citation_file_ticket,
    fetch_citation_image,
    issue_citation_file_ticket,
    principal_from_ticket,
    public_citation,
)
from enterprise.gateway.query.answer_split import (
    StreamThinkSplitter,
    public_reasoning,
    split_assistant_output,
)
from enterprise.gateway.query.citation_select import (
    sanitize_citation_markers,
    select_cited_chunk_refs,
)
from enterprise.gateway.query.diagnostics import (
    finish_trace,
    merge_upstream,
    record_timed_event,
    start_trace,
)
from enterprise.gateway.query.formal_router import (
    FormalScopeResolver,
    NO_RELIABLE_EVIDENCE_ANSWER,
    _FormalQueryError,
    _citation_document_allowed,
    _citation_document_for_principal,
    _chunk_to_citation,
    _conversation_lock,
    _ensure_chat,
    _ensure_chat_info,
    _query_client,
    _sse,
)
from enterprise.gateway.query.llm_provider_errors import classify_llm_provider_error
from enterprise.gateway.query.ragflow_client import RAGFlowAPIError
from enterprise.gateway.query.source_access import source_response
from enterprise.gateway.sync.models import ExtDocumentMap
from enterprise.gateway.sync.readiness import document_candidate_readiness_from_db
from enterprise.gateway.sync.transient_attachment import (
    ATTACHMENT_DISABLED_MESSAGE,
    TransientAttachmentError,
    attachment_max_size_bytes,
)


router = APIRouter(prefix="/enterprise/api/v2", tags=["query-v2"])
logger = logging.getLogger(__name__)

IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{3,127}")
PREVIOUS_COMPARISON_RE = re.compile(
    r"(?:刚才|上一台|前一台).{0,16}(?:比|比较|对比|区别|差异)"
)
# True equipment lookups (unknown ids fail-closed). Product models/serials do not.
EQUIPMENT_QUERY_CUE_RE = re.compile(
    r"(?:设备号|设备编号|固定资产号|设备是|"
    r"(?:查|查询|查找|检索).{0,32}"
    r"(?:资料|文档|档案|图纸|说明书|手册))"
)
# Labels that mark a nearby token as型号/出厂编号, not an equipment id.
MODEL_OR_SERIAL_LABEL_RE = re.compile(
    r"(?:产品型号|整机型号|规格型号|出厂编号|产品编号|型号)"
)
EQUIPMENT_ID_HINT = (
    "建议补充设备号或固定资产号，我可以只查该设备的资料，回答会更准确。"
)
# Kept for tests asserting retrieval questions never include this Gateway prefix.
GLOBAL_QUESTION_PREFIX = (
    "当前未指定具体设备，请仅根据检索到的资料回答用户问题。\n用户问题："
)
# Sent to RAGFlow when a message carries attachments but no user question;
# attachment bodies are parsed by RAGFlow itself from ``files[]``.
DEFAULT_ATTACHMENT_QUESTION = "请说明你上传的附件内容。"



async def _gw_read(gateway, fn, /, *args, **kwargs):
    from enterprise.gateway.db import GatewayDatabase
    if not isinstance(gateway, GatewayDatabase):
        return await fn(gateway, *args, **kwargs)
    async with gateway.transaction(write=False) as conn:
        return await fn(conn, *args, **kwargs)


async def _gw_write(gateway, fn, /, *args, **kwargs):
    from enterprise.gateway.db import GatewayDatabase
    if not isinstance(gateway, GatewayDatabase):
        return await fn(gateway, *args, **kwargs)
    async with gateway.transaction(write=True) as conn:
        return await fn(conn, *args, **kwargs)


async def get_db():
    from enterprise.gateway import app as app_module

    dep = app_module.app.dependency_overrides.get(app_module.get_db, app_module.get_db)
    return await dep()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    from enterprise.gateway.app import safe_error_message

    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": safe_error_message(code, message),
            "requestId": str(uuid.uuid4()),
            "retryable": code in {
                "RAGFLOW_UNAVAILABLE",
                "WEB_SEARCH_UNAVAILABLE",
                "ASSET_REGISTRY_UNAVAILABLE",
                "AUTH_REPLAY_STORE_UNAVAILABLE",
            },
        },
    )


def _mapped_llm_provider_failure(text: str) -> tuple[str, int, str] | None:
    """If text is a LiteLLM/Dashscope error payload, return safe failed-run fields."""
    classified = classify_llm_provider_error(text)
    if classified is None:
        return None
    from enterprise.gateway.app import safe_error_message

    code, status_code = classified
    logger.info(
        "llm provider error remapped code=%s err_type=provider_payload",
        code,
    )
    return code, status_code, safe_error_message(code)


def _public_run_payload(result: dict, citations: list[dict] | None = None) -> dict:
    payload = {
        key: v2_store.public_status(value) if key == "status" else value
        for key, value in result.items()
        if not str(key).startswith("_")
    }
    if citations is not None:
        payload["citations"] = citations
    return payload


def _citation_download_url(request: Request, citation_id: str, token: str) -> str:
    return str(
        request.url_for(
            "download_citation_file",
            citation_id=citation_id,
            ticket=token,
        )
    )


def _valid_web_url(value: Any) -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return url if parsed.scheme in {"http", "https"} and parsed.hostname else None


async def _citation_allowed(
    db,
    principal: UserPrincipal,
    citation: dict,
) -> bool:
    if citation.get("sourceType") == "web":
        return _valid_web_url(citation.get("url")) is not None
    return await _citation_document_allowed(db, principal, citation)


async def _project_citations(
    db,
    citations: list[dict],
    request: Request,
    principal: UserPrincipal,
) -> list[dict]:
    projected: list[dict] = []
    for item in citations:
        if item.get("sourceType") == "web":
            if _valid_web_url(item.get("url")):
                projected.append(
                    {
                        "citationId": item["citationId"],
                        "sourceType": "web",
                        "title": item.get("title") or "",
                        "externalDocumentId": None,
                        "sourceVersionId": None,
                        "pageNo": None,
                        "bbox": None,
                        "assetId": None,
                        "excerpt": item.get("excerpt"),
                        "recordType": None,
                        "recordId": None,
                        "url": item["url"],
                        "downloadUrl": None,
                        "downloadExpiresAt": None,
                        "refIndex": item.get("refIndex"),
                    }
                )
            continue
        ticket = await _gw_write(
            db, issue_citation_file_ticket, citation=item, principal=principal
        )
        projected.append(
            public_citation(
                item,
                ticket,
                _citation_download_url(request, item["citationId"], ticket.token),
            )
        )
    return projected


def _candidate_identifiers(question: str) -> list[str]:
    seen: list[str] = []
    for match in IDENTIFIER_RE.finditer(question or ""):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    return seen


def _device_like_identifiers(question: str) -> list[str]:
    return [
        token
        for token in _candidate_identifiers(question)
        if any(char.isalpha() for char in token)
        and (any(char.isdigit() for char in token) or any(char in "-_." for char in token))
    ]


def _token_near_model_or_serial(question: str, token: str) -> bool:
    """True when token sits next to 型号/出厂编号 — treat as product fact, not equipment id."""
    text = question or ""
    if not text or not token:
        return False
    for match in re.finditer(re.escape(token), text):
        window = text[max(0, match.start() - 24) : match.start()]
        if MODEL_OR_SERIAL_LABEL_RE.search(window):
            return True
    return False


def _has_equipment_query_cue(question: str) -> bool:
    return bool(EQUIPMENT_QUERY_CUE_RE.search(question or ""))


def _equipment_index(
    docs_by_internal_id: dict[str, ExtDocumentMap],
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for doc in docs_by_internal_id.values():
        if not doc.equipment_id:
            continue
        for value in (doc.equipment_id, doc.fixed_asset_no):
            if value:
                index.setdefault(value, set()).add(doc.equipment_id)
    return index


def _explicit_equipment_ids(
    question: str,
    docs_by_internal_id: dict[str, ExtDocumentMap],
) -> tuple[list[str], bool]:
    """Resolve explicit equipment ids from the question.

    A device-like token counts as an explicit equipment reference only when:
    - it uniquely hits the equipment index, or
    - it is unmatched AND the question has equipment-query cues
      (设备号/设备编号/固定资产号/查…资料/设备是).

    Tokens adjacent to 型号/出厂编号 are ignored so product-model confirmations
    (e.g. XT30D) do not clear turn scope. Unknown ids with cues fail-closed.
    """
    tokens = _device_like_identifiers(question)
    if not tokens:
        return [], False
    index = _equipment_index(docs_by_internal_id)
    has_cue = _has_equipment_query_cue(question)
    resolved: list[str] = []
    unresolved_explicit = False
    for token in tokens:
        if _token_near_model_or_serial(question, token):
            continue
        matches = index.get(token, set())
        if len(matches) == 1:
            equipment_id = next(iter(matches))
            if equipment_id not in resolved:
                resolved.append(equipment_id)
            continue
        if len(matches) > 1:
            return [], True
        # Unmatched: only fail-closed when the question is clearly an equipment lookup.
        if has_cue:
            unresolved_explicit = True
    if unresolved_explicit:
        return [], True
    return resolved, False


def _documents_for_equipment_ids(
    docs_by_internal_id: dict[str, ExtDocumentMap],
    equipment_ids: list[str],
    *,
    fixed_asset_no: str | None = None,
) -> dict[str, ExtDocumentMap]:
    if not equipment_ids:
        return docs_by_internal_id
    allowed = set(equipment_ids)
    selected = {
        document_id: doc
        for document_id, doc in docs_by_internal_id.items()
        if doc.equipment_id in allowed
    }
    # When EAM submits a single active equipment with fixedAssetNo, keep the
    # stricter identity match for that primary entity only.
    if fixed_asset_no and len(equipment_ids) == 1:
        selected = {
            document_id: doc
            for document_id, doc in selected.items()
            if doc.fixed_asset_no == fixed_asset_no
        }
    return selected


def _fixed_asset_for_equipment(
    docs_by_internal_id: dict[str, ExtDocumentMap], equipment_id: str
) -> str | None:
    values = {
        doc.fixed_asset_no
        for doc in docs_by_internal_id.values()
        if doc.equipment_id == equipment_id and doc.fixed_asset_no
    }
    return next(iter(values)) if len(values) == 1 else None


def _submitted_snapshot(
    equipment_id: str | None,
    fixed_asset_no: str | None,
    *,
    previous: dict | None = None,
) -> dict:
    if not equipment_id and not fixed_asset_no:
        return {
            "equipment_id": None,
            "fixed_asset_no": None,
            "asset_id": None,
            "registry_version": None,
            "context_resolved_at": (
                v2_store.utc_now()
                if previous
                and (previous.get("equipment_id") or previous.get("fixed_asset_no"))
                else None
            ),
        }
    keep = (
        previous is not None
        and previous.get("equipment_id") == equipment_id
        and previous.get("fixed_asset_no") == fixed_asset_no
    )
    return {
        "equipment_id": equipment_id,
        "fixed_asset_no": fixed_asset_no,
        "asset_id": previous.get("asset_id") if keep else None,
        "registry_version": previous.get("registry_version") if keep else None,
        "context_resolved_at": (
            previous.get("context_resolved_at") if keep else v2_store.utc_now()
        ),
    }


def _ragflow_question(conversation: dict, question: str) -> str:
    """Pass the user question through for retrieval scoring.

    Equipment identity is enforced by Gateway doc_ids scope and the enterprise
    Chat system prompt / document_metadata. Do not prepend Gateway identity
    text here — RAGFlow uses this same string for Dense/BM25 relevance.
    """
    return question


def _with_equipment_hint(conversation: dict, answer: str, status: str) -> str:
    if conversation.get("equipment_id") or status != "completed" or not answer.strip():
        return answer
    if EQUIPMENT_ID_HINT in answer:
        return answer
    return f"{answer.rstrip()}\n\n{EQUIPMENT_ID_HINT}"


def _allowed_identifiers(conversation: dict, question: str) -> list[str]:
    allowed: list[str] = []
    try:
        turn_ids = conversation["_turn_entity_ids"]
    except (KeyError, IndexError, TypeError):
        turn_ids = None
    for value in turn_ids or []:
        if value and value not in allowed:
            allowed.append(value)
    for key in ("equipment_id", "fixed_asset_no"):
        value = _conversation_value(conversation, key)
        if value and value not in allowed:
            allowed.append(value)
    del question
    return allowed


def _attachment_observation_texts(
    observations: list[AttachmentObservation],
) -> list[str]:
    texts: list[str] = []
    for item in observations:
        texts.extend(str(value) for value in item.text_spans if str(value).strip())
        texts.extend(str(value) for value in item.error_codes if str(value).strip())
        texts.extend(str(value) for value in item.equipment_codes if str(value).strip())
        texts.extend(str(value) for value in item.visible_values if str(value).strip())
    return texts


_REASONING_MODE_TO_INT = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "ultra": 4,
}
ReasoningMode = Literal["simple", "low", "medium", "high", "ultra"]


def _v2_completion_kwargs(
    conversation: dict,
    question: str,
    observations: list[AttachmentObservation],
    *,
    files: list[dict[str, Any]] | None = None,
    internet: bool = False,
    session_id: str | None = None,
    reasoning_mode: str = "simple",
) -> dict[str, Any]:
    # scope_identifiers mirrors allowed_identifiers so RAGFlow can inject a
    # generation-side identity block without mistaking the user question for a
    # device token (allowed_identifiers also feeds Identifier Guard grounding).
    identifiers = _allowed_identifiers(conversation, question)
    kwargs: dict[str, Any] = {
        "grounding_version": 1,
        "allowed_identifiers": identifiers,
        "scope_identifiers": list(identifiers),
    }
    if session_id:
        kwargs["session_id"] = session_id
    texts = _attachment_observation_texts(observations)
    if texts:
        kwargs["attachment_observations"] = texts
    if files:
        kwargs["files"] = files
    if internet:
        kwargs["internet"] = True
    mapped = _REASONING_MODE_TO_INT.get(reasoning_mode)
    if mapped is not None:
        kwargs["reasoning"] = mapped
    if config.rag_diagnostics_enabled:
        kwargs["enterprise_diagnostics"] = True
    return kwargs


def _conversation_value(conversation: dict, key: str) -> str:
    value = None
    if isinstance(conversation, dict):
        value = conversation.get(key)
    else:
        try:
            value = conversation[key]
        except (KeyError, IndexError, TypeError):
            value = None
    if isinstance(value, str) and value.strip():
        return value
    return ""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateConversationRequest(StrictModel):
    equipmentId: str | None = Field(default=None, max_length=128)
    fixedAssetNo: str | None = Field(default=None, max_length=128)
    faultCode: str | None = Field(default=None, max_length=128)


class PatchContextRequest(StrictModel):
    equipmentId: str | None = Field(default=None, max_length=128)
    fixedAssetNo: str | None = Field(default=None, max_length=128)
    faultCode: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("At least one context field is required")
        return self


class CreateMessageRequest(StrictModel):
    clientMessageId: str = Field(min_length=1, max_length=128)
    question: str | None = Field(default=None, min_length=1, max_length=8000)
    suggestionId: str | None = Field(default=None, min_length=1, max_length=128)
    contextVersion: int | None = Field(default=None, ge=0)
    internetEnabled: bool = False
    reasoningMode: ReasoningMode = "simple"

    @model_validator(mode="after")
    def exactly_one_branch(self):
        fields_set = self.model_fields_set
        question_branch = (
            self.question is not None
            and "suggestionId" not in fields_set
            and "contextVersion" not in fields_set
        )
        suggestion_branch = (
            self.suggestionId is not None
            and self.contextVersion is not None
            and "question" not in fields_set
        )
        if not (question_branch or suggestion_branch):
            raise ValueError("Use exactly one question or suggestion branch")
        return self


class MessageAttachmentMetadata(StrictModel):
    clientMessageId: str = Field(min_length=1, max_length=128)
    question: str | None = Field(default=None, max_length=8000)
    suggestionId: str | None = Field(default=None, min_length=1, max_length=128)
    contextVersion: int | None = Field(default=None, ge=0)
    internetEnabled: bool = False
    reasoningMode: ReasoningMode = "simple"


def _conversation_detail(row: dict) -> dict:
    summary = v2_store.conversation_payload(row)
    summary["context"] = {
        "equipmentId": row["equipment_id"],
        "fixedAssetNo": row["fixed_asset_no"],
        "faultCode": row["fault_code"],
        "contextVersion": row["context_version"],
        "registryVersion": row.get("registry_version"),
    }
    summary["suggestions"] = _suggestions(row)
    summary["contextCompacted"] = bool((row.get("context_summary") or "").strip())
    return summary


async def _owned_conversation(db, principal: UserPrincipal, conversation_id: str):
    return await _gw_read(
        db,
        v2_store.get_conversation,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )


@router.post("/conversations", status_code=201)
async def create_conversation(
    req: CreateConversationRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    snapshot = _submitted_snapshot(req.equipmentId, req.fixedAssetNo)
    row = await _gw_write(db, v2_store.create_conversation,
        conversation_id=str(uuid.uuid4()),
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        equipment_id=snapshot["equipment_id"],
        fixed_asset_no=snapshot["fixed_asset_no"],
        asset_id=snapshot["asset_id"],
        fault_code=req.faultCode,
        registry_version=snapshot["registry_version"],
        context_resolved_at=snapshot["context_resolved_at"],
    )
    return _conversation_detail(row)


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    try:
        items, next_cursor, has_more = await _gw_read(db, v2_store.list_conversations,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _error(422, "VALIDATION_ERROR", "Invalid cursor")
    return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    row = await _owned_conversation(db, principal, conversation_id)
    if not row:
        return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
    return _conversation_detail(row)


@router.patch("/conversations/{conversation_id}/context")
async def patch_context(
    conversation_id: str,
    req: PatchContextRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    lock = await _conversation_lock(conversation_id)
    async with lock:
        row = await _owned_conversation(db, principal, conversation_id)
        if not row:
            return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        if row["status"] == "archived":
            return _error(409, "CONVERSATION_ARCHIVED", "Conversation is archived")
        values = {
            "equipmentId": row["equipment_id"],
            "fixedAssetNo": row["fixed_asset_no"],
            "faultCode": row["fault_code"],
        }
        for field in req.model_fields_set:
            values[field] = getattr(req, field)
        snapshot = _submitted_snapshot(
            values["equipmentId"],
            values["fixedAssetNo"],
            previous=row,
        )
        changed = (
            snapshot["equipment_id"],
            snapshot["fixed_asset_no"],
            snapshot["asset_id"],
            values["faultCode"],
        ) != (
            row["equipment_id"],
            row["fixed_asset_no"],
            row.get("asset_id"),
            row["fault_code"],
        )
        updated = await _gw_write(db, v2_store.update_context,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            equipment_id=snapshot["equipment_id"],
            fixed_asset_no=snapshot["fixed_asset_no"],
            fault_code=values["faultCode"],
            context_version=row["context_version"] + int(changed),
            asset_id=snapshot["asset_id"],
            registry_version=snapshot["registry_version"],
            context_resolved_at=snapshot["context_resolved_at"],
            expected_context_version=row["context_version"],
        )
        if updated is None:
            return _error(
                409,
                "CONVERSATION_CONTEXT_CONFLICT",
                "Conversation context version changed",
            )
    return _conversation_detail(updated)


@router.post("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    lock = await _conversation_lock(conversation_id)
    async with lock:
        row = await _owned_conversation(db, principal, conversation_id)
        if not row:
            return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        row = await _gw_write(db, v2_store.archive_conversation,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
        )
    return _conversation_detail(row)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    request: Request,
    conversation_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("list_sessions", "view_citations")
    ),
):
    row = await _owned_conversation(db, principal, conversation_id)
    if not row:
        return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
    try:
        items, next_cursor, has_more = await _gw_read(db, v2_store.list_messages,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _error(422, "VALIDATION_ERROR", "Invalid cursor")
    authorized_items = []
    for item in items:
        citations = [
            citation
            for citation in item.get("citations", [])
            if await _citation_allowed(db, principal, citation)
        ]
        authorized_items.append(
            {
                **item,
                "citations": await _project_citations(
                    db, citations, request, principal
                ),
            }
        )
    return {
        "items": authorized_items,
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


def _suggestions(row: dict) -> list[dict]:
    version = row["context_version"]
    if row["equipment_id"] or row["fixed_asset_no"]:
        definitions = (
            ("inspect-fault", "检查当前故障", "请根据可靠文档说明当前故障的检查步骤。"),
            ("maintenance", "查看维护要求", "请根据可靠文档说明该设备的维护要求。"),
        )
    else:
        definitions = (
            ("describe-problem", "描述问题", "请描述需要查询的设备和故障现象。"),
        )
    return [
        {
            "suggestionId": suggestion_id,
            "label": label,
            "displayPrompt": prompt,
            "contextVersion": version,
            "expiresAt": None,
        }
        for suggestion_id, label, prompt in definitions
    ]


@router.get("/conversations/{conversation_id}/suggestions")
async def list_suggestions(
    conversation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    row = await _owned_conversation(db, principal, conversation_id)
    if not row:
        return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
    return {"items": _suggestions(row), "contextVersion": row["context_version"]}


async def _available_context_scope(
    db, principal: UserPrincipal, conversation: dict
) -> tuple[AclScope, dict[str, ExtDocumentMap]]:
    from enterprise.gateway.acl.context import AclContext
    from enterprise.gateway.acl.scope import compile_scope

    resolver = FormalScopeResolver(db)
    acl_scope = await compile_scope(AclContext(principal=principal), resolver)
    if acl_scope.is_empty:
        return acl_scope, {}
    filtered: dict[str, ExtDocumentMap] = {}
    for internal_id, doc in resolver._docs.items():
        readiness, _quality_status = await _gw_read(
            db, document_candidate_readiness_from_db, doc
        )
        if not readiness.retrievable:
            continue
        filtered[internal_id] = doc
    if not filtered:
        return AclScope.empty(acl_scope.policy_version), {}
    return (
        AclScope.materialized(
            tuple(sorted({doc.ragflow_dataset_id for doc in filtered.values()})),
            tuple(sorted(filtered)),
            policy_version=acl_scope.policy_version,
        ),
        filtered,
    )


async def _context_scope(
    db, principal: UserPrincipal, conversation: dict
) -> tuple[AclScope, dict[str, ExtDocumentMap]]:
    acl_scope, available = await _available_context_scope(db, principal, conversation)
    if acl_scope.is_empty:
        return acl_scope, {}
    if conversation.get("_turn_scope_resolved"):
        allowed = set(conversation.get("_turn_document_ids") or [])
        filtered = {
            document_id: doc
            for document_id, doc in available.items()
            if document_id in allowed
        }
    else:
        equipment = conversation["equipment_id"]
        fixed = conversation["fixed_asset_no"]
        asset_id = conversation.get("asset_id")
        filtered = {
            document_id: doc
            for document_id, doc in available.items()
            if not equipment
            or (
                doc.equipment_id == equipment
                and (not fixed or doc.fixed_asset_no == fixed)
                and (not asset_id or doc.asset_id == asset_id)
            )
        }
    if not filtered:
        return AclScope.empty(acl_scope.policy_version), {}
    return (
        AclScope.materialized(
            tuple(sorted({doc.ragflow_dataset_id for doc in filtered.values()})),
            tuple(sorted(filtered)),
            policy_version=acl_scope.policy_version,
        ),
        filtered,
    )


async def _resolve_turn_scope(
    db,
    principal: UserPrincipal,
    conversation: dict,
    question: str,
) -> dict:
    _scope, available = await _available_context_scope(db, principal, conversation)
    explicit, unresolved = _explicit_equipment_ids(question, available)
    recent_records = await _gw_read(db, v2_store.list_recent_entity_scopes,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        limit=2,
    )
    recent = [record["entity_ids"] for record in recent_records]
    context_resolved_at = conversation.get("context_resolved_at") or ""
    latest_scope_at = recent_records[0]["created_at"] if recent_records else ""
    context_is_newer = bool(context_resolved_at and context_resolved_at > latest_scope_at)
    conversation_fixed = conversation.get("fixed_asset_no")
    if unresolved:
        entity_ids: list[str] = []
        selected: dict[str, ExtDocumentMap] = {}
    elif explicit:
        entity_ids = explicit
        selected = _documents_for_equipment_ids(available, entity_ids)
    elif PREVIOUS_COMPARISON_RE.search(question or ""):
        entity_ids = []
        for scope in recent:
            for equipment_id in scope:
                if equipment_id not in entity_ids:
                    entity_ids.append(equipment_id)
                if len(entity_ids) == 2:
                    break
            if len(entity_ids) == 2:
                break
        if not entity_ids and conversation.get("equipment_id"):
            entity_ids = [conversation["equipment_id"]]
        selected = _documents_for_equipment_ids(
            available,
            entity_ids,
            fixed_asset_no=conversation_fixed if len(entity_ids) == 1 else None,
        )
    elif context_is_newer:
        entity_ids = (
            [conversation["equipment_id"]]
            if conversation.get("equipment_id")
            else []
        )
        selected = _documents_for_equipment_ids(
            available,
            entity_ids,
            fixed_asset_no=conversation_fixed,
        )
    elif recent:
        entity_ids = list(recent[0])
        selected = _documents_for_equipment_ids(
            available,
            entity_ids,
            fixed_asset_no=(
                conversation_fixed
                if len(entity_ids) == 1
                and entity_ids[0] == conversation.get("equipment_id")
                else None
            ),
        )
    elif conversation.get("equipment_id"):
        entity_ids = [conversation["equipment_id"]]
        selected = _documents_for_equipment_ids(
            available,
            entity_ids,
            fixed_asset_no=conversation_fixed,
        )
    else:
        entity_ids = []
        selected = available

    if explicit:
        primary = explicit[-1]
        fixed_asset_no = _fixed_asset_for_equipment(available, primary)
        changed = (
            conversation.get("equipment_id") != primary
            or conversation.get("fixed_asset_no") != fixed_asset_no
        )
        if changed:
            updated = await _gw_write(db, v2_store.update_context,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                equipment_id=primary,
                fixed_asset_no=fixed_asset_no,
                fault_code=conversation.get("fault_code"),
                context_version=conversation["context_version"] + 1,
                asset_id=None,
                registry_version=None,
                context_resolved_at=v2_store.utc_now(),
                expected_context_version=conversation["context_version"],
            )
            if updated is None:
                raise _FormalQueryError(
                    "CONVERSATION_CONTEXT_CONFLICT",
                    409,
                    "Conversation context version changed",
                )
            conversation = updated

    conversation["_turn_scope_resolved"] = True
    conversation["_turn_entity_ids"] = list(entity_ids)
    conversation["_turn_document_ids"] = sorted(selected)
    return conversation


def _external_citations(
    chunks: list[dict],
    docs_by_internal_id: dict[str, ExtDocumentMap],
    message_id: str,
    answer: str = "",
    status: str = "completed",
    internet_enabled: bool = False,
    attachment_document_ids: set[str] | None = None,
) -> list[dict]:
    cited_refs = select_cited_chunk_refs(answer, chunks, status)
    attachment_document_ids = attachment_document_ids or set()
    citations: list[dict] = []
    for ordinal, (chunk, ref_index) in enumerate(cited_refs):
        document_id = chunk.get("document_id") or chunk.get("doc_id")
        if str(document_id or "") in attachment_document_ids:
            continue
        doc = docs_by_internal_id.get(document_id)
        if doc is not None:
            citations.append(
                _chunk_to_citation(
                    chunk,
                    ordinal,
                    doc,
                    message_id,
                    ref_index=ref_index,
                )
            )
            continue
        url = _valid_web_url(chunk.get("url")) if internet_enabled else None
        if not url:
            raise _FormalQueryError(
                "RAGFLOW_SCOPE_VIOLATION",
                502,
                "RAGFlow retrieval returned an out-of-scope document",
            )
        chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")
        citation_id = chunk_id or f"web-{ordinal}"
        if message_id:
            citation_id = f"{citation_id}-{message_id[:8]}"
        citations.append(
            {
                "citationId": citation_id,
                "sourceType": "web",
                "title": str(
                    chunk.get("document_name")
                    or chunk.get("docnm_kwd")
                    or chunk.get("title")
                    or url
                ),
                "externalDocumentId": None,
                "sourceVersionId": None,
                "pageNo": None,
                "bbox": None,
                "assetId": None,
                "excerpt": chunk.get("content") or chunk.get("content_with_weight"),
                "recordType": None,
                "recordId": None,
                "url": url,
                "downloadUrl": None,
                "downloadExpiresAt": None,
                "refIndex": ref_index,
            }
        )
    return citations


def _web_search_configured(chat: dict) -> bool:
    prompt_config = chat.get("prompt_config") or {}
    provider = prompt_config.get("web_search_provider", "tavily")
    key_name = {
        "tavily": "tavily_api_key",
        "querit": "querit_api_key",
    }.get(provider)
    value = prompt_config.get(key_name) if key_name else None
    return isinstance(value, str) and bool(value.strip())


async def _ensure_ragflow_session(
    db,
    principal: UserPrincipal,
    conversation: dict,
    client,
    chat_id: str,
    run_id: str,
) -> str:
    existing = conversation.get("ragflow_session_id")
    if existing:
        return str(existing)
    lock = await _conversation_lock(conversation["conversation_id"])
    async with lock:
        current = await _owned_conversation(
            db, principal, conversation["conversation_id"]
        )
        existing = current.get("ragflow_session_id") if current else None
        if existing:
            conversation.update(current)
            return str(existing)
        name = (
            f"eam-{principal.business_user_id[:128]}-"
            f"{conversation['conversation_id']}-{run_id}"
        )
        created = await client.create_session(chat_id, name)
        data = created.get("data") if isinstance(created, dict) else None
        session_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        if not session_id:
            raise RAGFlowAPIError("Session id missing after create", 502)
        rowcount = await _gw_write(
            db,
            v2_store.claim_ragflow_session,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            ragflow_chat_id=chat_id,
            ragflow_session_id=session_id,
        )
        if rowcount != 1:
            current = await _owned_conversation(
                db, principal, conversation["conversation_id"]
            )
            session_id = str((current or {}).get("ragflow_session_id") or "")
            if not session_id:
                raise RAGFlowAPIError("Session mapping could not be saved", 502)
        conversation["ragflow_chat_id"] = chat_id
        conversation["ragflow_session_id"] = session_id
        return session_id


def _request_hash(
    req: CreateMessageRequest, pending: list[PendingAttachment] | None = None
) -> str:
    payload = req.model_dump(exclude_none=True)
    if pending:
        payload["attachments"] = sorted(
            (
                {
                    "fileName": item.file_name,
                    "mediaType": item.media_type,
                    "sha256": item.sha256,
                }
                for item in pending
            ),
            key=lambda item: (item["fileName"], item["mediaType"], item["sha256"]),
        )
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _business_status(completion: dict | None) -> str:
    """Trust only the explicit terminal status in ``data.status``.

    RF-PATCH-007: RAGFlow sends ``status`` in the JSON completion payload and
    the SSE final frame with exactly one of the three terminal values. Missing
    or invalid values are an upstream contract violation, never a reason to
    re-derive the state from the answer text or citations.
    """
    data = completion.get("data", {}) if isinstance(completion, dict) else {}
    explicit = data.get("status") if isinstance(data, dict) else None
    if explicit not in {"completed", "no_reliable_evidence", "failed"}:
        raise _FormalQueryError(
            "RAGFLOW_API_INCOMPATIBLE",
            502,
            "RAGFlow completion status missing or invalid",
        )
    return explicit


def _error_response_from_result(result: dict) -> JSONResponse:
    error = result["_error"]
    return JSONResponse(status_code=error["statusCode"], content=error["body"])


def _pending_response(conversation: dict, req: CreateMessageRequest, run: dict) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "conversationId": conversation["conversation_id"],
            "clientMessageId": req.clientMessageId,
            "runId": run["run_id"],
            "status": v2_store.public_status(run["status"]),
            "replayed": True,
        },
    )


async def _replay_or_pending(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    pending: list[PendingAttachment] | None = None,
) -> tuple[dict | None, JSONResponse | None]:
    run = await _gw_read(db, v2_store.get_message_run,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        client_message_id=req.clientMessageId,
    )
    if not run:
        return None, None
    if run["request_hash"] != _request_hash(req, pending):
        return None, _error(
            409,
            "CLIENT_MESSAGE_ID_CONFLICT",
            "clientMessageId was already used with a different payload",
        )
    if run["status"] == "running":
        run = await _gw_write(db, v2_store.mark_expired_run_interrupted,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            client_message_id=req.clientMessageId,
        ) or run
        if run["status"] == "running":
            return None, _pending_response(conversation, req, run)
    if run.get("result_json"):
        result = json.loads(run["result_json"])
        if "_error" in result:
            return None, _error_response_from_result(result)
        result = dict(result)
        result["replayed"] = True
        return result, None
    return None, _error(503, "RUN_INTERRUPTED", "Message run did not produce a durable result")


async def _prepare_message_run(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    pending: list[PendingAttachment] | None = None,
) -> tuple[dict, str, dict | None, JSONResponse | None]:
    pending = pending or []
    replay, response = await _replay_or_pending(db, principal, conversation, req, pending)
    if replay is not None or response is not None:
        return conversation, "", replay, response
    if req.suggestionId:
        if pending:
            return conversation, "", None, _error(
                422, "VALIDATION_ERROR", "Suggestions cannot include files"
            )
        if req.contextVersion != conversation["context_version"]:
            return conversation, "", None, _error(409, "SUGGESTION_STALE", "Suggestion context is stale")
        definition = next(
            (item for item in _suggestions(conversation) if item["suggestionId"] == req.suggestionId),
            None,
        )
        if not definition:
            return conversation, "", None, _error(404, "SUGGESTION_NOT_FOUND", "Suggestion not found")
        question = definition["displayPrompt"]
    else:
        question = req.question or ""
    conversation = await _resolve_turn_scope(
        db, principal, conversation, question
    )
    title = " ".join(question.split())[:80] or (
        pending[0].file_name if pending else "New conversation"
    )
    run = await _gw_write(db, v2_store.reserve_message_run,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        client_message_id=req.clientMessageId,
        request_hash=_request_hash(req, pending),
        run_id=str(uuid.uuid4()),
        user_message_id=str(uuid.uuid4()),
        assistant_message_id=str(uuid.uuid4()),
        question=question,
        title=title,
        entity_scope=conversation.get("_turn_entity_ids") or [],
        allowed_doc_ids=conversation.get("_turn_document_ids") or [],
    )
    if run is None:
        replay, response = await _replay_or_pending(db, principal, conversation, req, pending)
        return conversation, "", replay, response or _error(503, "RUN_INTERRUPTED", "Message run could not be reserved")
    return conversation, question, run, None


async def _save_failed_run(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    run: dict,
    assistant_message_id: str,
    *,
    code: str,
    status_code: int,
    message: str,
) -> JSONResponse:
    await _gw_write(db, v2_store.add_message,
        message_id=assistant_message_id,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        role="assistant",
        content="",
        status="failed",
        citations=[],
    )
    error_response = _error(status_code, code, message)
    failed_result = {
        "_error": {
            "statusCode": status_code,
            "body": json.loads(error_response.body),
        }
    }
    diagnostics = finish_trace(
        run.get("_diagnostics"), outcome="failed", error_code=code
    )
    if diagnostics:
        failed_result["_diagnostics"] = diagnostics
    await _gw_write(db, v2_store.complete_message_run,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        client_message_id=req.clientMessageId,
        result=failed_result,
        status="failed",
        assistant_message_id=assistant_message_id,
    )
    return error_response


async def _retrieval_question(
    db,
    principal: UserPrincipal,
    conversation: dict,
    question: str,
    pending: list[PendingAttachment],
) -> tuple[str, Any, list[AttachmentObservation]]:
    if not pending:
        return question, None, []
    client = _query_client()
    chat_id = None
    scope, _docs = await _context_scope(db, principal, conversation)
    if not scope.is_empty:
        chat_id = await _ensure_chat(client, principal, scope)
    observations = await observe_attachments(pending, client, chat_id, db)
    if not question.strip():
        question = DEFAULT_ATTACHMENT_QUESTION
    return enrich_question(question, observations), client, observations


async def _execute_json_run(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    question: str,
    run: dict,
    pending: list[PendingAttachment] | None = None,
    request: Request | None = None,
) -> tuple[dict | None, JSONResponse | None]:
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    pending = pending or []
    client = None
    if config.rag_diagnostics_enabled:
        try:
            run["_diagnostics"] = start_trace(
                run["run_id"],
                query=question,
                reasoning_mode=req.reasoningMode,
                stream=False,
            )
        except Exception:
            run["_diagnostics"] = None
    try:
        try:
            scope_started = perf_counter()
            question, client, observations = await _retrieval_question(
                db, principal, conversation, question, pending
            )
            scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
            record_timed_event(
                run.get("_diagnostics"),
                "scope",
                scope_started,
                {
                    "source": "gateway",
                    "stage": "gateway_scope",
                    "entityIds": conversation.get("_turn_entity_ids") or [],
                    "requestedDocumentIds": conversation.get("_turn_document_ids") or [],
                    "allowedDocumentIds": list(scope.document_ids),
                },
            )
            answer = NO_RELIABLE_EVIDENCE_ANSWER
            status = "no_reliable_evidence"
            citations: list[dict] = []
            reasoning: str | None = None
            if not scope.is_empty:
                client = client or _query_client()
                chat_id, chat = await _ensure_chat_info(client, principal, scope)
                effective_internet = req.internetEnabled and _web_search_configured(chat)
                session_id = await _ensure_ragflow_session(
                    db,
                    principal,
                    conversation,
                    client,
                    chat_id,
                    run["run_id"],
                )
                files = completion_files(
                    pending, vision=chat_is_vision_capable(chat)
                )
                completion_kwargs = _v2_completion_kwargs(
                    conversation,
                    question,
                    observations,
                    files=files,
                    internet=effective_internet,
                    session_id=session_id,
                    reasoning_mode=req.reasoningMode,
                )
                upstream_started = perf_counter()
                upstream_status = "success"
                try:
                    completion = await client.chat_completion(
                        chat_id,
                        _ragflow_question(conversation, question),
                        doc_ids=list(scope.document_ids),
                        request_id=run["run_id"],
                        **completion_kwargs,
                    )
                except Exception:
                    upstream_status = "failed"
                    raise
                finally:
                    record_timed_event(
                        run.get("_diagnostics"),
                        "upstream_request",
                        upstream_started,
                        {
                            "source": "gateway",
                            "stage": "ragflow_request",
                            "status": upstream_status,
                        },
                    )
                data = completion.get("data", {}) if isinstance(completion, dict) else {}
                if isinstance(data, dict):
                    merge_upstream(run.get("_diagnostics"), data.get("_diagnostics"))
                reference = data.get("reference", {}) if isinstance(data, dict) else {}
                chunks = [
                    item
                    for item in (
                        reference.get("chunks", [])
                        if isinstance(reference, dict)
                        else []
                    )
                    if isinstance(item, dict)
                ]
                split = split_assistant_output(str(data.get("answer") or ""))
                mapped = _mapped_llm_provider_failure(split.answer)
                if mapped is not None:
                    code, status_code, message = mapped
                    return None, await _save_failed_run(
                        db, principal, conversation, req, run, assistant_message_id,
                        code=code, status_code=status_code, message=message,
                    )
                answer = sanitize_citation_markers(split.answer)
                reasoning = public_reasoning(split.reasoning)
                status = _business_status(completion)
                citations = _external_citations(
                    chunks,
                    docs_by_internal_id,
                    assistant_message_id,
                    answer=answer,
                    status=status,
                    internet_enabled=effective_internet,
                    attachment_document_ids={
                        str((item.ragflow_file or {}).get("id") or "")
                        for item in pending
                        if (item.ragflow_file or {}).get("id")
                    },
                )
                if status == "completed":
                    answer = _with_equipment_hint(conversation, answer, status)
                else:
                    # Defensive: a contract-violating upstream that reports
                    # no_reliable_evidence/failed together with cited markers
                    # must not persist the standard abstain text next to
                    # dangling citations (mirrors the v1 router).
                    citations = []
                    answer = NO_RELIABLE_EVIDENCE_ANSWER
            await _gw_write(db, v2_store.add_message,
                message_id=assistant_message_id,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                role="assistant",
                content=answer,
                status=status,
                citations=citations,
                reasoning=reasoning,
            )
            result = {
                "conversationId": conversation["conversation_id"],
                "clientMessageId": req.clientMessageId,
                "runId": run["run_id"],
                "messageId": assistant_message_id,
                "answer": answer,
                "reasoning": reasoning,
                "status": status,
                "citations": citations,
                "replayed": False,
            }
            if pending:
                result["attachments"] = [
                    _attachment_public_meta(item)
                    for item in pending
                    if item.attachment_id
                ]
            diagnostics = finish_trace(
                run.get("_diagnostics"), outcome=status
            )
            if diagnostics:
                result["_diagnostics"] = diagnostics
            await _gw_write(db, v2_store.complete_message_run,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                client_message_id=req.clientMessageId,
                result=result,
                status="completed",
                assistant_message_id=assistant_message_id,
            )
            return result, None
        except (RAGFlowAPIError, _FormalQueryError) as exc:
            if isinstance(exc, _FormalQueryError):
                code, status_code, message = exc.code, exc.status_code, exc.message
            else:
                mapped = _mapped_llm_provider_failure(str(exc))
                if mapped is not None:
                    code, status_code, message = mapped
                else:
                    code = (
                        "RAGFLOW_API_INCOMPATIBLE"
                        if exc.status_code
                        and (400 <= exc.status_code < 500 or exc.status_code == 502)
                        else "RAGFLOW_UNAVAILABLE"
                    )
                    status_code = 503
                    message = "Query engine unavailable"
            return None, await _save_failed_run(
                db, principal, conversation, req, run, assistant_message_id,
                code=code, status_code=status_code, message=message,
            )
        except Exception as exc:
            logger.exception("json message run failed err_type=%s", type(exc).__name__)
            return None, await _save_failed_run(
                db, principal, conversation, req, run, assistant_message_id,
                code="INTERNAL_ERROR", status_code=500, message="Message run failed",
            )
    finally:
        await cleanup_ragflow_files(pending, client, db)


async def _stream_run_events(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    question: str,
    run: dict,
    pending: list[PendingAttachment] | None = None,
    request: Request | None = None,
) -> AsyncIterator[str]:
    conversation_id = conversation["conversation_id"]
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    if config.rag_diagnostics_enabled:
        try:
            run["_diagnostics"] = start_trace(
                run["run_id"],
                query=question,
                reasoning_mode=req.reasoningMode,
                stream=True,
            )
        except Exception:
            run["_diagnostics"] = None
    yield _sse(
        "run.started",
        {
            "conversationId": conversation_id,
            "clientMessageId": req.clientMessageId,
            "runId": run["run_id"],
            "replayed": False,
        },
    )
    accumulated = ""
    accumulated_reasoning = ""
    chunks: list[dict] = []
    citations: list[dict] = []
    upstream_status: str | None = None
    status = "no_reliable_evidence"
    answer = NO_RELIABLE_EVIDENCE_ANSWER
    reasoning: str | None = None
    splitter = StreamThinkSplitter()
    pending = pending or []
    client = None
    live_streamed = False
    emitted_answer = ""
    first_stream_output_recorded = False
    try:
        scope_started = perf_counter()
        question, client, observations = await _retrieval_question(
            db, principal, conversation, question, pending
        )
        scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
        record_timed_event(
            run.get("_diagnostics"),
            "scope",
            scope_started,
            {
                "source": "gateway",
                "stage": "gateway_scope",
                "entityIds": conversation.get("_turn_entity_ids") or [],
                "requestedDocumentIds": conversation.get("_turn_document_ids") or [],
                "allowedDocumentIds": list(scope.document_ids),
            },
        )
        if not scope.is_empty:
            client = client or _query_client()
            chat_id, chat = await _ensure_chat_info(client, principal, scope)
            effective_internet = req.internetEnabled and _web_search_configured(chat)
            session_id = await _ensure_ragflow_session(
                db,
                principal,
                conversation,
                client,
                chat_id,
                run["run_id"],
            )
            files = completion_files(
                pending, vision=chat_is_vision_capable(chat)
            )
            stream_kwargs = _v2_completion_kwargs(
                conversation,
                question,
                observations,
                files=files,
                internet=effective_internet,
                session_id=session_id,
                reasoning_mode=req.reasoningMode,
            )
            upstream_started = perf_counter()
            async def iter_upstream():
                upstream_status = "success"
                try:
                    async for payload in client.chat_completion_stream(
                        chat_id,
                        _ragflow_question(conversation, question),
                        doc_ids=list(scope.document_ids),
                        request_id=run["run_id"],
                        **stream_kwargs,
                    ):
                        yield payload
                except Exception:
                    upstream_status = "failed"
                    raise
                finally:
                    record_timed_event(
                        run.get("_diagnostics"),
                        "upstream_request",
                        upstream_started,
                        {
                            "source": "gateway",
                            "stage": "ragflow_request",
                            "status": upstream_status,
                        },
                    )

            async for payload in iter_upstream():
                data = payload.get("data") if isinstance(payload, dict) else None
                if data is True:
                    break
                if not isinstance(data, dict):
                    continue
                merge_upstream(run.get("_diagnostics"), data.get("_diagnostics"))
                explicit_status = data.get("status")
                if explicit_status in {"completed", "no_reliable_evidence", "failed"}:
                    upstream_status = explicit_status
                reference = data.get("reference") or {}
                raw_chunks = (
                    reference.get("chunks", [])
                    if isinstance(reference, dict)
                    else []
                )
                chunks.extend(item for item in raw_chunks if isinstance(item, dict))
                delta = data.get("answer")
                if (
                    not first_stream_output_recorded
                    and not data.get("final")
                    and (delta or data.get("start_to_think") or data.get("end_to_think"))
                ):
                    record_timed_event(
                        run.get("_diagnostics"),
                        "stream_first_token",
                        upstream_started,
                        {
                            "source": "gateway",
                            "stage": "stream_first_token",
                            "status": "success",
                        },
                    )
                    first_stream_output_recorded = True
                if not data.get("final"):
                    for kind, chunk in splitter.feed(
                        str(delta or ""),
                        start_to_think=bool(data.get("start_to_think")),
                        end_to_think=bool(data.get("end_to_think")),
                    ):
                        if kind == "reasoning":
                            accumulated_reasoning += chunk
                            event = "reasoning.delta"
                        else:
                            accumulated += chunk
                            emitted_answer += chunk
                            event = "answer.delta"
                        live_streamed = True
                        yield _sse(
                            event,
                            {
                                "conversationId": conversation_id,
                                "runId": run["run_id"],
                                "content": chunk,
                            },
                        )
                elif delta and not accumulated and not accumulated_reasoning:
                    split = split_assistant_output(str(delta))
                    accumulated = split.answer
                    accumulated_reasoning = split.reasoning

            conversation["ragflow_chat_id"] = chat_id
            conversation["ragflow_session_id"] = session_id
            mapped = _mapped_llm_provider_failure(accumulated)
            if mapped is not None:
                code, status_code, message = mapped
                await _save_failed_run(
                    db, principal, conversation, req, run, assistant_message_id,
                    code=code, status_code=status_code, message=message,
                )
                if live_streamed and emitted_answer:
                    yield _sse(
                        "answer.replaced",
                        {
                            "conversationId": conversation_id,
                            "runId": run["run_id"],
                            "content": "",
                        },
                    )
                yield _sse(
                    "run.failed",
                    {
                        "conversationId": conversation_id,
                        "runId": run["run_id"],
                        "code": code,
                        "message": message,
                    },
                )
                return
            accumulated = sanitize_citation_markers(accumulated)
            if upstream_status not in {"completed", "no_reliable_evidence", "failed"}:
                raise _FormalQueryError(
                    "RAGFLOW_API_INCOMPATIBLE",
                    502,
                    "RAGFlow completion status missing or invalid",
                )
            status = upstream_status
            reasoning = public_reasoning(accumulated_reasoning)
            citations = _external_citations(
                chunks,
                docs_by_internal_id,
                assistant_message_id,
                answer=accumulated,
                status=status,
                internet_enabled=effective_internet,
                attachment_document_ids={
                    str((item.ragflow_file or {}).get("id") or "")
                    for item in pending
                    if (item.ragflow_file or {}).get("id")
                },
            )
            if status == "completed":
                answer = _with_equipment_hint(conversation, accumulated, status)
            else:
                # Defensive: never stream/persist citations next to the
                # replaced standard abstain answer (mirrors the v1 router).
                citations = []
                answer = NO_RELIABLE_EVIDENCE_ANSWER
        await _gw_write(db, v2_store.add_message,
            message_id=assistant_message_id,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            role="assistant",
            content=answer,
            status=status,
            citations=citations,
            reasoning=reasoning,
        )
        stream_deltas: list[dict] = []
        if reasoning:
            stream_deltas.append({"event": "reasoning.delta", "content": reasoning})
        stream_deltas.append({"event": "answer.delta", "content": answer})
        result = {
            "conversationId": conversation_id,
            "clientMessageId": req.clientMessageId,
            "runId": run["run_id"],
            "messageId": assistant_message_id,
            "answer": answer,
            "reasoning": reasoning,
            "status": status,
            "citations": citations,
            "replayed": False,
            "_streamDeltas": stream_deltas,
        }
        if pending:
            result["attachments"] = [
                _attachment_public_meta(item)
                for item in pending
                if item.attachment_id
            ]
        diagnostics = finish_trace(run.get("_diagnostics"), outcome=status)
        if diagnostics:
            result["_diagnostics"] = diagnostics
        await _gw_write(db, v2_store.complete_message_run,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            client_message_id=req.clientMessageId,
            result=result,
            status="completed",
            assistant_message_id=assistant_message_id,
        )
        public_citations = (
            await _project_citations(db, citations, request, principal)
            if request is not None
            else citations
        )
        if live_streamed:
            if answer != emitted_answer:
                yield _sse(
                    "answer.replaced",
                    {
                        "conversationId": conversation_id,
                        "runId": run["run_id"],
                        "content": answer,
                    },
                )
        else:
            for delta in stream_deltas:
                yield _sse(
                    str(delta.get("event") or "answer.delta"),
                    {
                        "conversationId": conversation_id,
                        "runId": run["run_id"],
                        "content": delta.get("content"),
                    },
                )
        for citation in public_citations:
            yield _sse("citation", citation)
        yield _sse(
            "answer.completed",
            {
                "conversationId": conversation_id,
                "runId": run["run_id"],
                "messageId": assistant_message_id,
                "status": v2_store.public_status(status),
                "citations": public_citations,
            },
        )
    except asyncio.CancelledError:
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="RUN_INTERRUPTED", status_code=503,
            message="Message run was interrupted before completion",
        )
        raise
    except (RAGFlowAPIError, _FormalQueryError) as exc:
        if isinstance(exc, _FormalQueryError):
            code, message = exc.code, exc.message
            status_code = exc.status_code
        else:
            mapped = _mapped_llm_provider_failure(str(exc))
            if mapped is not None:
                code, status_code, message = mapped
            else:
                code = (
                    "RAGFLOW_API_INCOMPATIBLE"
                    if exc.status_code
                    and (400 <= exc.status_code < 500 or exc.status_code == 502)
                    else "RAGFLOW_UNAVAILABLE"
                )
                message = "Query engine unavailable"
                status_code = 503
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code=code, status_code=status_code, message=message,
        )
        if live_streamed and emitted_answer:
            yield _sse(
                "answer.replaced",
                {
                    "conversationId": conversation_id,
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield _sse(
            "run.failed",
            {
                "conversationId": conversation_id,
                "runId": run["run_id"],
                "code": code,
                "message": message,
            },
        )
    except (httpx.HTTPError, asyncio.TimeoutError, ConnectionError, OSError):
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="RAGFLOW_UNAVAILABLE", status_code=503,
            message="Query engine unavailable",
        )
        if live_streamed and emitted_answer:
            yield _sse(
                "answer.replaced",
                {
                    "conversationId": conversation_id,
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield _sse(
            "run.failed",
            {
                "conversationId": conversation_id,
                "runId": run["run_id"],
                "code": "RAGFLOW_UNAVAILABLE",
                "message": "Query engine unavailable",
            },
        )
    except Exception as exc:
        logger.exception("sse message run failed err_type=%s", type(exc).__name__)
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="INTERNAL_ERROR", status_code=500, message="Message run failed",
        )
        if live_streamed and emitted_answer:
            yield _sse(
                "answer.replaced",
                {
                    "conversationId": conversation_id,
                    "runId": run["run_id"],
                    "content": "",
                },
            )
        yield _sse(
            "run.failed",
            {
                "conversationId": conversation_id,
                "runId": run["run_id"],
                "code": "INTERNAL_ERROR",
                "message": "Message run failed",
            },
        )
    finally:
        await cleanup_ragflow_files(pending, client, db)


async def _result_events(result: dict) -> AsyncIterator[str]:
    yield _sse(
        "run.started",
        {
            "conversationId": result["conversationId"],
            "clientMessageId": result["clientMessageId"],
            "runId": result.get("runId"),
            "replayed": result["replayed"],
        },
    )
    for delta in result.get("_streamDeltas", []):
        if isinstance(delta, dict):
            event = str(delta.get("event") or "answer.delta")
            content = delta.get("content")
        else:
            event = "answer.delta"
            content = delta
        yield _sse(
            event,
            {
                "conversationId": result["conversationId"],
                "runId": result.get("runId"),
                "content": content,
            },
        )
    if not result.get("_streamDeltas"):
        yield _sse(
            "answer.delta",
            {
                "conversationId": result["conversationId"],
                "runId": result.get("runId"),
                "content": result.get("answer", ""),
            },
        )
    for citation in result["citations"]:
        yield _sse("citation", citation)
    yield _sse(
        "answer.completed",
        {
            "conversationId": result["conversationId"],
            "runId": result.get("runId"),
            "messageId": result["messageId"],
            "status": v2_store.public_status(result["status"]),
            "citations": result["citations"],
        },
    )


def _attachment_public_meta(item: PendingAttachment) -> dict:
    payload = {
        "attachmentId": item.attachment_id,
        "fileName": item.file_name,
        "mediaType": item.media_type,
        "sizeBytes": item.size_bytes,
        "sha256": item.sha256,
    }
    return payload


def _inquiry_audit_body(req: CreateMessageRequest, pending: list[PendingAttachment]) -> dict:
    return {
        "clientMessageId": req.clientMessageId,
        "hasAttachments": bool(pending),
        "attachments": [
            {
                "fileName": item.file_name,
                "mediaType": item.media_type,
                "sizeBytes": item.size_bytes,
                "sha256": item.sha256[:12],
                **({"attachmentId": item.attachment_id} if item.attachment_id else {}),
            }
            for item in pending
        ],
    }


async def _read_upload_bytes(upload, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(65536)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise TransientAttachmentError(
                "ATTACHMENT_TOO_LARGE",
                413,
                "Attachment exceeds the configured size limit",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise ValueError("Attachment content is empty")
    return content


async def _parse_multipart_message(
    request: Request,
) -> tuple[CreateMessageRequest, list[PendingAttachment]]:
    form = await request.form()
    try:
        raw_meta = form.get("metadata")
        if raw_meta is None or raw_meta == "":
            raise ValueError("metadata is required")
        if hasattr(raw_meta, "read"):
            raw_meta = (await raw_meta.read()).decode("utf-8", "replace")
        meta = MessageAttachmentMetadata.model_validate(json.loads(str(raw_meta)))
        if meta.suggestionId is not None or meta.contextVersion is not None:
            raise ValueError("Suggestions cannot include files")
        uploads = [item for item in form.getlist("files") if item not in (None, "")]
        if len(uploads) > MAX_MESSAGE_FILES:
            raise ValueError("At most 5 files are allowed")
        pending: list[PendingAttachment] = []
        max_bytes = attachment_max_size_bytes()
        for upload in uploads:
            media_type = (
                (getattr(upload, "content_type", None) or "").split(";")[0].strip().lower()
            )
            if media_type not in MESSAGE_MEDIA_TYPES:
                raise ValueError("Attachment MIME type is not allowed")
            filename = ragflow_attachment_filename(
                getattr(upload, "filename", None), media_type
            )
            content = await _read_upload_bytes(upload, max_bytes)
            pending.append(
                PendingAttachment(
                    file_name=filename,
                    media_type=media_type,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    attachment_id=str(uuid.uuid4()),
                )
            )
        question = (meta.question or "").strip() or None
        if not question and not pending:
            raise ValueError("question or files is required")
        req = CreateMessageRequest.model_construct(
            clientMessageId=meta.clientMessageId,
            question=question,
            internetEnabled=meta.internetEnabled,
            reasoningMode=meta.reasoningMode,
        )
        return req, pending
    finally:
        close = getattr(form, "close", None)
        if close is not None:
            await close()


async def _persist_pending_attachments(
    db,
    run: dict,
    pending: list[PendingAttachment],
) -> list[dict]:
    metas = [_attachment_public_meta(item) for item in pending]
    user_message_id = run.get("user_message_id")
    if user_message_id and metas:
        await _gw_write(db, v2_store.set_message_attachments, message_id=user_message_id, attachments=metas
        )
    return metas


@router.post("/conversations/{conversation_id}/messages")
async def create_message(
    conversation_id: str,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("ask", "view_citations")
    ),
):
    content_type = (request.headers.get("content-type") or "").lower()
    pending: list[PendingAttachment] = []
    if "multipart/form-data" in content_type:
        conversation = await _owned_conversation(db, principal, conversation_id)
        if not conversation:
            return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        if conversation["status"] == "archived":
            return _error(409, "CONVERSATION_ARCHIVED", "Conversation is archived")
        if not config.transient_attachments_enabled:
            return _error(
                503, "ATTACHMENT_STORAGE_UNAVAILABLE", ATTACHMENT_DISABLED_MESSAGE
            )
        try:
            req, pending = await _parse_multipart_message(request)
        except TransientAttachmentError as exc:
            return _error(exc.status_code, exc.code, exc.message)
        except (ValidationError, json.JSONDecodeError, ValueError):
            return _error(422, "VALIDATION_ERROR", "Invalid multipart message")
        request.state.inquiry_audit_body = _inquiry_audit_body(req, pending)
    else:
        try:
            payload = await request.json()
        except Exception:
            return _error(422, "VALIDATION_ERROR", "Invalid JSON")
        try:
            req = CreateMessageRequest.model_validate(payload)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc

    lock = await _conversation_lock(conversation_id)
    async with lock:
        conversation = await _owned_conversation(db, principal, conversation_id)
        if not conversation:
            return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        if conversation["status"] == "archived":
            return _error(409, "CONVERSATION_ARCHIVED", "Conversation is archived")
        try:
            conversation, question, run_or_result, error = await _prepare_message_run(
                db, principal, conversation, req, pending
            )
        except _FormalQueryError as exc:
            return _error(exc.status_code, exc.code, exc.message)
        if error:
            return error
        if run_or_result is None:
            return _error(503, "RUN_INTERRUPTED", "Message run could not be prepared")
        if "answer" in run_or_result and "messageId" in run_or_result:
            result = run_or_result
            public_citations = await _project_citations(
                db, result.get("citations") or [], request, principal
            )
            if "text/event-stream" in request.headers.get("accept", "").lower():
                return StreamingResponse(
                    _result_events({**result, "citations": public_citations}),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return _public_run_payload(result, public_citations)
        run = run_or_result
        if pending:
            await _persist_pending_attachments(db, run, pending)
            request.state.inquiry_audit_body = _inquiry_audit_body(req, pending)
    if run.get("status") == "running" and not question and not pending:
        return _pending_response(conversation, req, run)
    if "text/event-stream" in request.headers.get("accept", "").lower():
        return StreamingResponse(
            _stream_run_events(
                db, principal, conversation, req, question, run, pending, request
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result, error = await _execute_json_run(
        db, principal, conversation, req, question, run, pending, request
    )
    if error:
        return error
    public_citations = await _project_citations(
        db, result.get("citations") or [], request, principal
    )
    return _public_run_payload(result, public_citations)


@router.get("/citations/{citation_id}")
async def get_citation(
    citation_id: str,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("view_citations", "list_sessions")
    ),
):
    citation = await _gw_read(db, v2_store.get_citation,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not citation:
        return _error(404, "CITATION_NOT_FOUND", "Citation not found")
    if not await _citation_allowed(db, principal, citation):
        return _error(403, "ACL_DENIED", "Access denied")
    projected = await _project_citations(db, [citation], request, principal)
    return projected[0]


@router.get(
    "/citations/{citation_id}/file/{ticket}",
    name="download_citation_file",
)
async def download_citation_file(
    citation_id: str,
    ticket: str,
    request: Request,
    db=Depends(get_db),
):
    try:
        claimed = await _gw_write(db, claim_citation_file_ticket, citation_id, ticket)
    except CitationFileError:
        return _error(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")
    citation = await _gw_read(db, v2_store.get_citation,
        citation_id=citation_id,
        tenant_id=claimed["tenant_id"],
        business_user_id=claimed["business_user_id"],
    )
    if not citation:
        return _error(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")
    principal = principal_from_ticket(claimed)
    doc = await _citation_document_for_principal(db, principal, citation)
    if doc is None:
        return _error(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")
    if claimed["kind"] == "crop":
        image_id = claimed.get("image_id") or citation.get("imageId") or ""
        fetched = await fetch_citation_image(str(image_id))
        if not fetched:
            return _error(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")
        content, media_type = fetched
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )
    return await source_response(request, doc)


@router.get("/citations/{citation_id}/source")
async def get_citation_source(
    citation_id: str,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("view_citations", "list_sessions")
    ),
):
    citation = await _gw_read(db, v2_store.get_citation,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    doc = await _citation_document_for_principal(db, principal, citation) if citation else None
    if doc is None:
        return _error(404, "CITATION_NOT_FOUND", "Citation not found")
    return await source_response(request, doc)
