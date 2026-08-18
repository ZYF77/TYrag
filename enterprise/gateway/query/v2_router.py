"""Frozen v2 conversation API for the Equipment Management System."""
from __future__ import annotations

import hashlib
import json
import asyncio
import re
import uuid
from typing import Any, AsyncIterator

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
    MESSAGE_MEDIA_TYPES,
    MAX_MESSAGE_FILES,
    PendingAttachment,
    any_understood,
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
    force_abstain_outcome,
    select_cited_chunks,
)
from enterprise.gateway.query.formal_router import (
    FormalScopeResolver,
    NO_RELIABLE_EVIDENCE_ANSWER,
    _FormalQueryError,
    _build_citations,
    _citation_document_allowed,
    _citation_document_for_principal,
    _conversation_lock,
    _ensure_chat,
    _ensure_chat_info,
    _query_client,
    _sse,
)
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

IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{3,127}")
EQUIPMENT_ID_HINT = (
    "建议补充设备号或固定资产号（例如 GD01250002），我可以只查该设备的资料，回答会更准确。"
)
# Kept for tests asserting retrieval questions never include this Gateway prefix.
GLOBAL_QUESTION_PREFIX = (
    "当前未指定具体设备，请仅根据检索到的资料回答用户问题。\n用户问题："
)


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
                "ASSET_REGISTRY_UNAVAILABLE",
                "AUTH_REPLAY_STORE_UNAVAILABLE",
            },
        },
    )


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


async def _project_citations(
    db,
    citations: list[dict],
    request: Request,
    principal: UserPrincipal,
) -> list[dict]:
    projected: list[dict] = []
    for item in citations:
        ticket = await issue_citation_file_ticket(
            db, citation=item, principal=principal
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


async def _lookup_equipment_metadata(
    db, tenant_id: str, tokens: list[str]
) -> tuple[str | None, str | None]:
    if not tokens:
        return None, None
    placeholders = ",".join("?" * len(tokens))
    async with db.execute(
        f"""SELECT equipment_id, fixed_asset_no
            FROM ext_document_map
            WHERE tenant_id=?
              AND (equipment_id IN ({placeholders})
                   OR fixed_asset_no IN ({placeholders}))""",
        (tenant_id, *tokens, *tokens),
    ) as cursor:
        rows = await cursor.fetchall()
    equipment_ids = {row["equipment_id"] for row in rows if row["equipment_id"]}
    if len(equipment_ids) != 1:
        return None, None
    equipment_id = next(iter(equipment_ids))
    fixed_nos = {
        row["fixed_asset_no"]
        for row in rows
        if row["equipment_id"] == equipment_id and row["fixed_asset_no"]
    }
    fixed_asset_no = next(iter(fixed_nos)) if len(fixed_nos) == 1 else None
    return equipment_id, fixed_asset_no


async def _bind_from_question(
    db, principal: UserPrincipal, conversation: dict, question: str
) -> dict:
    equipment_id, fixed_asset_no = await _lookup_equipment_metadata(
        db, principal.tenant_id, _candidate_identifiers(question)
    )
    if not equipment_id:
        return conversation
    updated = await v2_store.update_context(
        db,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        equipment_id=equipment_id,
        fixed_asset_no=fixed_asset_no,
        fault_code=conversation.get("fault_code"),
        context_version=conversation["context_version"] + 1,
        asset_id=None,
        registry_version=None,
        context_resolved_at=v2_store.utc_now(),
        expected_context_version=conversation["context_version"],
    )
    return updated or conversation


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
            "context_resolved_at": None,
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
    await v2_store.ensure_schema(db)
    return await v2_store.get_conversation(
        db,
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
    await v2_store.ensure_schema(db)
    snapshot = _submitted_snapshot(req.equipmentId, req.fixedAssetNo)
    row = await v2_store.create_conversation(
        db,
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
    await v2_store.ensure_schema(db)
    try:
        items, next_cursor, has_more = await v2_store.list_conversations(
            db,
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
        has_started = row.get("first_message_at") is not None
        bound = bool(row["equipment_id"])
        if has_started and bound and values["equipmentId"] != row["equipment_id"]:
            return _error(
                409,
                "CONVERSATION_CONTEXT_STALE",
                "Canonical equipment cannot change after the first message",
            )
        if has_started and bound and not values["equipmentId"]:
            return _error(
                409,
                "CONVERSATION_CONTEXT_STALE",
                "Canonical equipment cannot be cleared after the first message",
            )
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
        updated = await v2_store.update_context(
            db,
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
        row = await v2_store.archive_conversation(
            db,
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
        items, next_cursor, has_more = await v2_store.list_messages(
            db,
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
            if await _citation_document_allowed(db, principal, citation)
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


async def _context_scope(
    db, principal: UserPrincipal, conversation: dict
) -> tuple[AclScope, dict[str, ExtDocumentMap]]:
    from enterprise.gateway.acl.context import AclContext
    from enterprise.gateway.acl.scope import compile_scope

    resolver = FormalScopeResolver(db)
    acl_scope = await compile_scope(AclContext(principal=principal), resolver)
    if acl_scope.is_empty:
        return acl_scope, {}
    equipment = conversation["equipment_id"]
    fixed = conversation["fixed_asset_no"]
    asset_id = conversation.get("asset_id")
    filtered: dict[str, ExtDocumentMap] = {}
    for internal_id, doc in resolver._docs.items():
        if equipment and doc.equipment_id != equipment:
            continue
        if equipment and fixed and doc.fixed_asset_no != fixed:
            continue
        if equipment and asset_id and doc.asset_id != asset_id:
            continue
        readiness, _quality_status = await document_candidate_readiness_from_db(
            db, doc
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


def _external_citations(
    chunks: list[dict],
    docs_by_internal_id: dict[str, ExtDocumentMap],
    message_id: str,
    answer: str = "",
    status: str = "completed",
) -> list[dict]:
    cited = select_cited_chunks(answer, chunks, status)
    return _build_citations(cited, docs_by_internal_id, message_id)


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


def _business_status(completion: dict | None, answer: str) -> str:
    """Resolve business state without consulting citation presence."""
    data = completion.get("data", {}) if isinstance(completion, dict) else {}
    explicit = data.get("status") if isinstance(data, dict) else None
    if explicit in {"completed", "no_reliable_evidence", "failed"}:
        status = explicit
    else:
        status = "completed" if answer.strip() else "no_reliable_evidence"
    return force_abstain_outcome(answer, status)


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
    run = await v2_store.get_message_run(
        db,
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
        run = await v2_store.mark_expired_run_interrupted(
            db,
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
    if not conversation.get("equipment_id"):
        conversation = await _bind_from_question(db, principal, conversation, question)
    title = " ".join(question.split())[:80] or (
        pending[0].file_name if pending else "New conversation"
    )
    run = await v2_store.reserve_message_run(
        db,
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
    content: str = "",
) -> JSONResponse:
    await v2_store.add_message(
        db,
        message_id=assistant_message_id,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        role="assistant",
        content=content,
        status="failed",
        citations=[],
    )
    error_response = _error(status_code, code, message)
    await v2_store.complete_message_run(
        db,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        client_message_id=req.clientMessageId,
        result={
            "_error": {
                "statusCode": status_code,
                "body": json.loads(error_response.body),
            }
        },
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
) -> tuple[str, JSONResponse | None, Any]:
    if not pending:
        return question, None, None
    client = _query_client()
    chat_id = None
    scope, _docs = await _context_scope(db, principal, conversation)
    if not scope.is_empty:
        chat_id = await _ensure_chat(client, principal, scope)
    observations = await observe_attachments(pending, client, chat_id, db)
    if not question.strip() and not any_understood(observations):
        return question, _error(
            422,
            "VALIDATION_ERROR",
            "Could not understand the attachment; add a text question",
        ), client
    return enrich_question(question, observations), None, client


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
    try:
        try:
            question, observe_error, client = await _retrieval_question(
                db, principal, conversation, question, pending
            )
            if observe_error:
                return None, await _save_failed_run(
                    db, principal, conversation, req, run, assistant_message_id,
                    code="VALIDATION_ERROR", status_code=422,
                    message="Could not understand the attachment; add a text question",
                )
            scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
            answer = NO_RELIABLE_EVIDENCE_ANSWER
            status = "no_reliable_evidence"
            citations: list[dict] = []
            reasoning: str | None = None
            if not scope.is_empty:
                client = client or _query_client()
                chat_id, chat = await _ensure_chat_info(client, principal, scope)
                files = completion_files(
                    pending, vision=chat_is_vision_capable(chat)
                )
                completion_kwargs: dict[str, Any] = {}
                if files:
                    completion_kwargs["files"] = files
                completion = await client.chat_completion(
                    chat_id,
                    _ragflow_question(conversation, question),
                    session_id=conversation.get("ragflow_session_id"),
                    doc_ids=list(scope.document_ids),
                    **completion_kwargs,
                )
                data = completion.get("data", {}) if isinstance(completion, dict) else {}
                reference = data.get("reference", {}) if isinstance(data, dict) else {}
                chunks = [item for item in (reference.get("chunks", []) if isinstance(reference, dict) else []) if isinstance(item, dict)]
                split = split_assistant_output(str(data.get("answer") or ""))
                answer = split.answer
                reasoning = public_reasoning(split.reasoning)
                status = _business_status(completion, answer)
                citations = _external_citations(
                    chunks,
                    docs_by_internal_id,
                    assistant_message_id,
                    answer=answer,
                    status=status,
                )
                if status == "no_reliable_evidence" and not answer:
                    answer = NO_RELIABLE_EVIDENCE_ANSWER
                answer = _with_equipment_hint(conversation, answer, status)
                await db.execute(
                    """UPDATE ext_v2_conversation
                       SET ragflow_chat_id=?, ragflow_session_id=COALESCE(?, ragflow_session_id)
                       WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
                    (chat_id, data.get("session_id"), conversation["conversation_id"], principal.tenant_id, principal.business_user_id),
                )
                await db.commit()
            await v2_store.add_message(
                db,
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
            await v2_store.complete_message_run(
                db,
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
                code = "RAGFLOW_API_INCOMPATIBLE" if exc.status_code and 400 <= exc.status_code < 500 else "RAGFLOW_UNAVAILABLE"
                status_code = 503
                message = "Query engine unavailable"
            return None, await _save_failed_run(
                db, principal, conversation, req, run, assistant_message_id,
                code=code, status_code=status_code, message=message,
            )
        except Exception:
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
    yield _sse("run.started", {"conversationId": conversation_id, "clientMessageId": req.clientMessageId, "runId": run["run_id"], "replayed": False})
    accumulated = ""
    accumulated_reasoning = ""
    deltas: list[dict] = []
    chunks: list[dict] = []
    citations: list[dict] = []
    upstream_status: str | None = None
    status = "no_reliable_evidence"
    answer = NO_RELIABLE_EVIDENCE_ANSWER
    reasoning: str | None = None
    splitter = StreamThinkSplitter()
    pending = pending or []
    client = None
    try:
        question, observe_error, client = await _retrieval_question(
            db, principal, conversation, question, pending
        )
        if observe_error:
            await _save_failed_run(
                db, principal, conversation, req, run, assistant_message_id,
                code="VALIDATION_ERROR", status_code=422,
                message="Could not understand the attachment; add a text question",
            )
            yield _sse("run.failed", {"conversationId": conversation_id, "runId": run["run_id"], "code": "VALIDATION_ERROR", "message": "Could not understand the attachment; add a text question"})
            return
        scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
        if not scope.is_empty:
            client = client or _query_client()
            chat_id, chat = await _ensure_chat_info(client, principal, scope)
            session_id = conversation.get("ragflow_session_id")
            ragflow_session_id = session_id
            files = completion_files(
                pending, vision=chat_is_vision_capable(chat)
            )
            stream_kwargs: dict[str, Any] = {}
            if files:
                stream_kwargs["files"] = files
            async for payload in client.chat_completion_stream(
                chat_id,
                _ragflow_question(conversation, question),
                session_id=session_id,
                doc_ids=list(scope.document_ids),
                **stream_kwargs,
            ):
                data = payload.get("data") if isinstance(payload, dict) else None
                if data is True:
                    break
                if not isinstance(data, dict):
                    continue
                explicit_status = data.get("status")
                if explicit_status in {"completed", "no_reliable_evidence", "failed"}:
                    upstream_status = explicit_status
                ragflow_session_id = data.get("session_id") or ragflow_session_id
                reference = data.get("reference") or {}
                raw_chunks = reference.get("chunks", []) if isinstance(reference, dict) else []
                chunks.extend(item for item in raw_chunks if isinstance(item, dict))
                delta = data.get("answer")
                if not data.get("final"):
                    pieces = splitter.feed(
                        str(delta or ""),
                        start_to_think=bool(data.get("start_to_think")),
                        end_to_think=bool(data.get("end_to_think")),
                    )
                    for kind, chunk in pieces:
                        event = (
                            "reasoning.delta"
                            if kind == "reasoning"
                            else "answer.delta"
                        )
                        if kind == "reasoning":
                            accumulated_reasoning += chunk
                        else:
                            accumulated += chunk
                        deltas.append({"event": event, "content": chunk})
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
                await db.execute(
                    """UPDATE ext_v2_conversation
                       SET ragflow_chat_id=?, ragflow_session_id=COALESCE(?, ragflow_session_id)
                       WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
                    (chat_id, ragflow_session_id, conversation_id, principal.tenant_id, principal.business_user_id),
                )
                await db.commit()
            status = force_abstain_outcome(
                accumulated,
                upstream_status
                or (
                    "completed"
                    if accumulated.strip()
                    else "no_reliable_evidence"
                ),
            )
            citations = _external_citations(
                chunks,
                docs_by_internal_id,
                assistant_message_id,
                answer=accumulated,
                status=status,
            )
            answer = accumulated
            reasoning = public_reasoning(accumulated_reasoning)
            if status == "no_reliable_evidence" and not answer:
                answer = NO_RELIABLE_EVIDENCE_ANSWER
            hinted = _with_equipment_hint(conversation, answer, status)
            extra = hinted[len(answer):] if hinted.startswith(answer) else ""
            if extra:
                deltas.append({"event": "answer.delta", "content": extra})
                yield _sse(
                    "answer.delta",
                    {
                        "conversationId": conversation_id,
                        "runId": run["run_id"],
                        "content": extra,
                    },
                )
            answer = hinted
        await v2_store.add_message(
            db,
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
            "_streamDeltas": deltas,
        }
        await v2_store.complete_message_run(
            db,
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
        for citation in public_citations:
            yield _sse("citation", citation)
        yield _sse("answer.completed", {"conversationId": conversation_id, "runId": run["run_id"], "messageId": assistant_message_id, "status": v2_store.public_status(status), "citations": public_citations})
    except asyncio.CancelledError:
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="RUN_INTERRUPTED", status_code=503,
            message="Message run was interrupted before completion", content=accumulated,
        )
        raise
    except (RAGFlowAPIError, _FormalQueryError) as exc:
        if isinstance(exc, _FormalQueryError):
            code, message = exc.code, exc.message
        else:
            code = "RAGFLOW_API_INCOMPATIBLE" if exc.status_code and 400 <= exc.status_code < 500 else "RAGFLOW_UNAVAILABLE"
            message = "Query engine unavailable"
        status_code = exc.status_code if isinstance(exc, _FormalQueryError) else 503
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code=code, status_code=status_code, message=message, content=accumulated,
        )
        yield _sse("run.failed", {"conversationId": conversation_id, "runId": run["run_id"], "code": code, "message": message})
    except (httpx.HTTPError, asyncio.TimeoutError, ConnectionError, OSError):
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="RAGFLOW_UNAVAILABLE", status_code=503,
            message="Query engine unavailable", content=accumulated,
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
    except Exception:
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="INTERNAL_ERROR", status_code=500, message="Message run failed", content=accumulated,
        )
        yield _sse("run.failed", {"conversationId": conversation_id, "runId": run["run_id"], "code": "INTERNAL_ERROR", "message": "Message run failed"})
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
        await v2_store.set_message_attachments(
            db, message_id=user_message_id, attachments=metas
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
    if result is not None and pending:
        result = dict(result)
        result["attachments"] = [
            _attachment_public_meta(item) for item in pending if item.attachment_id
        ]
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
    await v2_store.ensure_schema(db)
    citation = await v2_store.get_citation(
        db,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not citation:
        return _error(404, "CITATION_NOT_FOUND", "Citation not found")
    if not await _citation_document_allowed(db, principal, citation):
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
    await v2_store.ensure_schema(db)
    try:
        claimed = await claim_citation_file_ticket(db, citation_id, ticket)
    except CitationFileError:
        return _error(404, "CITATION_FILE_NOT_FOUND", "Citation file not found")
    citation = await v2_store.get_citation(
        db,
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
    await v2_store.ensure_schema(db)
    citation = await v2_store.get_citation(
        db,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    doc = await _citation_document_for_principal(db, principal, citation) if citation else None
    if doc is None:
        return _error(404, "CITATION_NOT_FOUND", "Citation not found")
    return await source_response(request, doc)
