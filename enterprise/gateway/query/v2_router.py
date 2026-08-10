"""Frozen v2 conversation API for the Equipment Management System."""
from __future__ import annotations

import hashlib
import json
import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.gateway.acl.schema import AclScope
from enterprise.gateway.asset_registry import (
    ASSET_REGISTRY_TTL_SECONDS,
    AssetRegistryConflict,
    AssetRegistryError,
    AssetRegistryInvalid,
    AssetRegistryUnavailable,
    ResolvedAsset,
    resolve_asset,
)
from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query import v2_store
from enterprise.gateway.query.formal_router import (
    FormalScopeResolver,
    NO_RELIABLE_EVIDENCE_ANSWER,
    _FormalQueryError,
    _build_citations,
    _conversation_lock,
    _ensure_chat,
    _query_client,
    _sse,
)
from enterprise.gateway.query.ragflow_client import RAGFlowAPIError
from enterprise.gateway.sync.models import ExtDocumentMap


router = APIRouter(prefix="/enterprise/api/v2", tags=["query-v2"])


async def get_db():
    from enterprise.gateway import app as app_module

    dep = app_module.app.dependency_overrides.get(app_module.get_db, app_module.get_db)
    return await dep()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "requestId": str(uuid.uuid4()),
            "retryable": code in {
                "RAGFLOW_UNAVAILABLE",
                "ASSET_REGISTRY_UNAVAILABLE",
                "AUTH_REPLAY_STORE_UNAVAILABLE",
            },
        },
    )


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
        question_branch = (
            self.question is not None
            and self.suggestionId is None
            and self.contextVersion is None
        )
        suggestion_branch = (
            self.question is None
            and self.suggestionId is not None
            and self.contextVersion is not None
        )
        if not (question_branch or suggestion_branch):
            raise ValueError("Use exactly one question or suggestion branch")
        return self


def _conversation_detail(row: dict) -> dict:
    summary = v2_store.conversation_payload(row)
    summary["context"] = {
        "equipmentId": row["equipment_id"],
        "fixedAssetNo": row["fixed_asset_no"],
        "faultCode": row["fault_code"],
        "contextVersion": row["context_version"],
        "registryVersion": row.get("registry_version"),
    }
    return summary


async def _owned_conversation(db, principal: UserPrincipal, conversation_id: str):
    await v2_store.ensure_schema(db)
    return await v2_store.get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )


async def _validate_context_response(
    db,
    *,
    tenant_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    asset_id: str | None = None,
) -> tuple[ResolvedAsset | None, JSONResponse | None]:
    if not any((equipment_id, fixed_asset_no, asset_id)):
        return None, None
    try:
        return await resolve_asset(
            db,
            tenant_id=tenant_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
            asset_id=asset_id,
        ), None
    except AssetRegistryConflict:
        return None, _error(409, "CONVERSATION_CONTEXT_CONFLICT", "Equipment identifiers do not resolve to the same Asset Registry identity")
    except AssetRegistryInvalid:
        return None, _error(422, "CONVERSATION_CONTEXT_INVALID", "Equipment identifier was not found in the Asset Registry")
    except AssetRegistryUnavailable:
        return None, _error(503, "ASSET_REGISTRY_UNAVAILABLE", "Asset Registry is temporarily unavailable")
    except AssetRegistryError:
        return None, _error(503, "ASSET_REGISTRY_UNAVAILABLE", "Asset Registry is temporarily unavailable")


@router.post("/conversations", status_code=201)
async def create_conversation(
    req: CreateConversationRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    await v2_store.ensure_schema(db)
    canonical, error = await _validate_context_response(
        db,
        tenant_id=principal.tenant_id,
        equipment_id=req.equipmentId,
        fixed_asset_no=req.fixedAssetNo,
    )
    if error:
        return error
    resolved = canonical
    row = await v2_store.create_conversation(
        db,
        conversation_id=str(uuid.uuid4()),
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        equipment_id=resolved.equipment_id if resolved else None,
        fixed_asset_no=resolved.fixed_asset_no if resolved else None,
        fault_code=req.faultCode,
        registry_version=resolved.registry_version if resolved else None,
        context_resolved_at=resolved.resolved_at if resolved else None,
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
        if has_started and "equipmentId" in req.model_fields_set:
            if values["equipmentId"] != row["equipment_id"]:
                return _error(
                    409,
                    "CONVERSATION_CONTEXT_STALE",
                    "Canonical equipment cannot change after the first message",
                )
        if has_started and "fixedAssetNo" in req.model_fields_set:
            # A fixed asset alias may change only when the registry still
            # resolves it to the immutable canonical equipment.
            if values["fixedAssetNo"] != row["fixed_asset_no"]:
                candidate_equipment = row["equipment_id"]
            else:
                candidate_equipment = values["equipmentId"]
        else:
            candidate_equipment = values["equipmentId"]
        if has_started and not candidate_equipment:
            return _error(
                409,
                "CONVERSATION_CONTEXT_STALE",
                "Canonical equipment cannot be cleared after the first message",
            )
        canonical, error = await _validate_context_response(
            db,
            tenant_id=principal.tenant_id,
            equipment_id=candidate_equipment,
            fixed_asset_no=values["fixedAssetNo"],
        )
        if error:
            return error
        if canonical:
            equipment_id, fixed_asset_no = (
                canonical.equipment_id,
                canonical.fixed_asset_no,
            )
            registry_version = canonical.registry_version
            context_resolved_at = canonical.resolved_at
        else:
            equipment_id = fixed_asset_no = registry_version = context_resolved_at = None
        changed = (
            equipment_id,
            fixed_asset_no,
            values["faultCode"],
        ) != (row["equipment_id"], row["fixed_asset_no"], row["fault_code"])
        updated = await v2_store.update_context(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
            fault_code=values["faultCode"],
            context_version=row["context_version"] + int(changed),
            registry_version=registry_version,
            context_resolved_at=context_resolved_at,
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
    return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}


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
    # A draft conversation is intentionally not a global query.  It can be
    # displayed and edited, but the first message must establish a canonical
    # Asset Registry identity.
    if not equipment:
        return AclScope.empty(acl_scope.policy_version), {}
    filtered: dict[str, ExtDocumentMap] = {}
    for internal_id, doc in resolver._docs.items():
        aliases = {
            value
            for value in (
                getattr(doc, "equipment_id", None),
                getattr(doc, "fixed_asset_no", None),
                doc.asset_id,
            )
            if value
        }
        if equipment not in aliases:
            continue
        if fixed and fixed not in aliases:
            continue
        if not doc.current_version:
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


async def _refresh_context_snapshot(
    db, principal: UserPrincipal, conversation: dict
) -> dict:
    """Refresh a stale snapshot and reject silent Asset Registry rebinds."""
    if not conversation.get("equipment_id"):
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_REQUIRED",
            422,
            "A canonical equipment context is required before sending a message",
        )
    resolved_at = conversation.get("context_resolved_at")
    stale = True
    if resolved_at:
        try:
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(resolved_at)
            ).total_seconds()
            stale = age > ASSET_REGISTRY_TTL_SECONDS
        except ValueError:
            stale = True
    if not stale:
        return conversation
    try:
        resolved = await resolve_asset(
            db,
            tenant_id=principal.tenant_id,
            equipment_id=conversation["equipment_id"],
            fixed_asset_no=conversation.get("fixed_asset_no"),
        )
    except AssetRegistryConflict:
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_STALE",
            409,
            "Conversation context no longer matches the Asset Registry",
        ) from None
    except AssetRegistryInvalid:
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_STALE",
            409,
            "Conversation context no longer resolves in the Asset Registry",
        ) from None
    except AssetRegistryUnavailable:
        raise _FormalQueryError(
            "ASSET_REGISTRY_UNAVAILABLE",
            503,
            "Asset Registry is temporarily unavailable",
        ) from None
    if resolved.equipment_id != conversation["equipment_id"]:
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_STALE",
            409,
            "Canonical equipment cannot change after the first message",
        )
    if (
        resolved.fixed_asset_no != conversation.get("fixed_asset_no")
        or resolved.registry_version != conversation.get("registry_version")
    ):
        conversation = await v2_store.update_context(
            db,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            equipment_id=resolved.equipment_id,
            fixed_asset_no=resolved.fixed_asset_no,
            fault_code=conversation.get("fault_code"),
            context_version=conversation["context_version"],
            registry_version=resolved.registry_version,
            context_resolved_at=resolved.resolved_at,
        )
    return conversation


def _external_citations(
    chunks: list[dict], docs_by_internal_id: dict[str, ExtDocumentMap], message_id: str
) -> list[dict]:
    internal = _build_citations(chunks, docs_by_internal_id, message_id)
    return [
        {
            "citationId": item["citationId"],
            "sourceType": item.get("sourceType", "document"),
            "title": item["title"],
            "externalDocumentId": item.get("documentId"),
            "sourceVersionId": item.get("versionId"),
            "pageNo": item.get("pageNo"),
            "bbox": item.get("bbox"),
            "assetId": item.get("assetId"),
            "excerpt": item.get("excerpt"),
            "recordType": item.get("recordType"),
            "recordId": item.get("recordId"),
        }
        for item in internal
    ]


def _request_hash(req: CreateMessageRequest) -> str:
    raw = json.dumps(
        req.model_dump(exclude_none=True),
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
        return explicit
    return "completed" if answer.strip() else "no_reliable_evidence"


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
            "status": run["status"],
            "replayed": True,
        },
    )


async def _replay_or_pending(
    db, principal: UserPrincipal, conversation: dict, req: CreateMessageRequest
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
    if run["request_hash"] != _request_hash(req):
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
) -> tuple[dict, str, dict | None, JSONResponse | None]:
    replay, response = await _replay_or_pending(db, principal, conversation, req)
    if replay is not None or response is not None:
        return conversation, "", replay, response
    conversation = await _refresh_context_snapshot(db, principal, conversation)
    if req.suggestionId:
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
    run = await v2_store.reserve_message_run(
        db,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        client_message_id=req.clientMessageId,
        request_hash=_request_hash(req),
        run_id=str(uuid.uuid4()),
        user_message_id=str(uuid.uuid4()),
        assistant_message_id=str(uuid.uuid4()),
        question=question,
    )
    if run is None:
        replay, response = await _replay_or_pending(db, principal, conversation, req)
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


async def _execute_json_run(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    question: str,
    run: dict,
) -> tuple[dict | None, JSONResponse | None]:
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    try:
        scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
        answer = NO_RELIABLE_EVIDENCE_ANSWER
        status = "no_reliable_evidence"
        citations: list[dict] = []
        if not scope.is_empty:
            client = _query_client()
            chat_id = await _ensure_chat(client, principal, scope)
            completion = await client.chat_completion(
                chat_id,
                question,
                session_id=conversation.get("ragflow_session_id"),
                doc_ids=list(scope.document_ids),
            )
            data = completion.get("data", {}) if isinstance(completion, dict) else {}
            reference = data.get("reference", {}) if isinstance(data, dict) else {}
            chunks = [item for item in (reference.get("chunks", []) if isinstance(reference, dict) else []) if isinstance(item, dict)]
            answer = str(data.get("answer") or "")
            status = _business_status(completion, answer)
            citations = _external_citations(chunks, docs_by_internal_id, assistant_message_id)
            if status == "no_reliable_evidence" and not answer:
                answer = NO_RELIABLE_EVIDENCE_ANSWER
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
        )
        result = {
            "conversationId": conversation["conversation_id"],
            "clientMessageId": req.clientMessageId,
            "runId": run["run_id"],
            "messageId": assistant_message_id,
            "answer": answer,
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
            status_code = 502 if code.endswith("INCOMPATIBLE") else 503
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


async def _stream_run_events(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: CreateMessageRequest,
    question: str,
    run: dict,
) -> AsyncIterator[str]:
    conversation_id = conversation["conversation_id"]
    assistant_message_id = run.get("assistant_message_id") or str(uuid.uuid4())
    yield _sse("run.started", {"conversationId": conversation_id, "clientMessageId": req.clientMessageId, "runId": run["run_id"], "replayed": False})
    accumulated = ""
    chunks: list[dict] = []
    citations: list[dict] = []
    status = "no_reliable_evidence"
    answer = NO_RELIABLE_EVIDENCE_ANSWER
    try:
        scope, docs_by_internal_id = await _context_scope(db, principal, conversation)
        if not scope.is_empty:
            client = _query_client()
            chat_id = await _ensure_chat(client, principal, scope)
            session_id = conversation.get("ragflow_session_id")
            ragflow_session_id = session_id
            async for payload in client.chat_completion_stream(
                chat_id, question, session_id=session_id, doc_ids=list(scope.document_ids)
            ):
                data = payload.get("data") if isinstance(payload, dict) else None
                if data is True:
                    break
                if not isinstance(data, dict):
                    continue
                ragflow_session_id = data.get("session_id") or ragflow_session_id
                reference = data.get("reference") or {}
                raw_chunks = reference.get("chunks", []) if isinstance(reference, dict) else []
                chunks.extend(item for item in raw_chunks if isinstance(item, dict))
                delta = data.get("answer")
                if delta and not data.get("final"):
                    accumulated += str(delta)
                    yield _sse("answer.delta", {"conversationId": conversation_id, "runId": run["run_id"], "content": str(delta)})
                elif delta and data.get("final") and not accumulated:
                    accumulated = str(delta)
                await db.execute(
                    """UPDATE ext_v2_conversation
                       SET ragflow_chat_id=?, ragflow_session_id=COALESCE(?, ragflow_session_id)
                       WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
                    (chat_id, ragflow_session_id, conversation_id, principal.tenant_id, principal.business_user_id),
                )
                await db.commit()
            status = "completed" if accumulated.strip() else "no_reliable_evidence"
            citations = _external_citations(chunks, docs_by_internal_id, assistant_message_id)
            answer = accumulated if status == "completed" else NO_RELIABLE_EVIDENCE_ANSWER
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
        )
        result = {
            "conversationId": conversation_id,
            "clientMessageId": req.clientMessageId,
            "runId": run["run_id"],
            "messageId": assistant_message_id,
            "answer": answer,
            "status": status,
            "citations": citations,
            "replayed": False,
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
        for citation in citations:
            yield _sse("citation", citation)
        yield _sse("answer.completed", {"conversationId": conversation_id, "runId": run["run_id"], "messageId": assistant_message_id, "status": status, "citations": citations})
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
        status_code = 502 if code.endswith("INCOMPATIBLE") else (503 if code != "CONVERSATION_CONTEXT_STALE" else 409)
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code=code, status_code=status_code, message=message, content=accumulated,
        )
        yield _sse("run.failed", {"conversationId": conversation_id, "runId": run["run_id"], "code": code, "message": message})
    except Exception:
        await _save_failed_run(
            db, principal, conversation, req, run, assistant_message_id,
            code="INTERNAL_ERROR", status_code=500, message="Message run failed", content=accumulated,
        )
        yield _sse("run.failed", {"conversationId": conversation_id, "runId": run["run_id"], "code": "INTERNAL_ERROR", "message": "Message run failed"})


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
    if result["answer"]:
        yield _sse(
            "answer.delta",
            {"conversationId": result["conversationId"], "runId": result.get("runId"), "content": result["answer"]},
        )
    for citation in result["citations"]:
        yield _sse("citation", citation)
    yield _sse(
        "answer.completed",
        {
            "conversationId": result["conversationId"],
            "runId": result.get("runId"),
            "messageId": result["messageId"],
            "status": result["status"],
            "citations": result["citations"],
        },
    )


@router.post("/conversations/{conversation_id}/messages")
async def create_message(
    conversation_id: str,
    req: CreateMessageRequest,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("ask", "view_citations")
    ),
):
    lock = await _conversation_lock(conversation_id)
    async with lock:
        conversation = await _owned_conversation(db, principal, conversation_id)
        if not conversation:
            return _error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        if conversation["status"] == "archived":
            return _error(409, "CONVERSATION_ARCHIVED", "Conversation is archived")
        try:
            conversation, question, run_or_result, error = await _prepare_message_run(
                db, principal, conversation, req
            )
        except _FormalQueryError as exc:
            return _error(exc.status_code, exc.code, exc.message)
    if error:
        return error
    if run_or_result is None:
        return _error(503, "RUN_INTERRUPTED", "Message run could not be prepared")
    if "answer" in run_or_result and "messageId" in run_or_result:
        result = run_or_result
        if "text/event-stream" in request.headers.get("accept", "").lower():
            return StreamingResponse(
                _result_events(result),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return result
    run = run_or_result
    if run.get("status") == "running" and not question:
        return _pending_response(conversation, req, run)
    if "text/event-stream" in request.headers.get("accept", "").lower():
        return StreamingResponse(
            _stream_run_events(db, principal, conversation, req, question, run),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result, error = await _execute_json_run(
        db, principal, conversation, req, question, run
    )
    return error or result


@router.get("/citations/{citation_id}")
async def get_citation(
    citation_id: str,
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
    resolver = FormalScopeResolver(db)
    from enterprise.gateway.acl.context import AclContext
    from enterprise.gateway.acl.scope import compile_scope

    await compile_scope(AclContext(principal=principal), resolver)
    allowed = any(
        doc.external_document_id == citation.get("externalDocumentId")
        and doc.source_version_id == citation.get("sourceVersionId")
        for doc in resolver._docs.values()
    )
    if not allowed:
        return _error(403, "ACL_DENIED", "Access denied")
    return citation
