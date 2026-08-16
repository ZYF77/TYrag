"""Formal WP-04 query, conversation, SSE and citation API.

This router is the production replacement for the temporary demo query loop.
It keeps RAGFlow IDs internal, persists message content and citation
snapshots, and enforces ownership plus document ACL on every entry point.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.acl.policy import evaluate_document_acl
from enterprise.gateway.acl.schema import AclScope, DocumentAclFacts
from enterprise.gateway.acl.scope import ScopeResolver, compile_scope
from enterprise.gateway.asset_registry import (
    AssetRegistryConflict,
    AssetRegistryError,
    AssetRegistryInvalid,
    AssetRegistryUnavailable,
    ResolvedAsset,
    resolve_asset,
)
from enterprise.gateway.auth.middleware import require_capability
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import require_ragflow_api_key
from enterprise.gateway.quality.gate import enforce_quality_gate
from enterprise.gateway.quality.models import get_latest_evaluation
from enterprise.gateway.query import conversation_store
from enterprise.gateway.query.enterprise_prompt import (
    enterprise_prompt_config_for_api,
    needs_enterprise_prompt_upgrade,
)
from enterprise.gateway.query.ragflow_client import (
    RAGFlowAPIError,
    RAGFlowQueryClient,
    RAGFlowQueryStub,
)
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    get_versions_for_document,
    list_all_mappings,
)
from enterprise.gateway.query.source_access import source_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1", tags=["query"])

NO_RELIABLE_EVIDENCE_ANSWER = "未找到可靠依据，无法回答。"


def _query_source_system() -> str | None:
    """Return the configured query source system, or None to include all.

    Tests default to DEMO. Production must not silently drop EAM documents
    when ENTERPRISE_QUERY_SOURCE_SYSTEM is unset.
    """
    explicit = (os.environ.get("ENTERPRISE_QUERY_SOURCE_SYSTEM") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
        return "DEMO"
    return None


_query_stub: RAGFlowQueryStub | None = None
_conversation_locks: dict[str, asyncio.Lock] = {}


async def get_db():
    """Reuse the app-level SQLite connection without importing app eagerly."""
    from enterprise.gateway import app as app_module

    dep = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    return await dep()


def _query_client():
    global _query_stub
    if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
        if _query_stub is None:
            _query_stub = RAGFlowQueryStub()
        return _query_stub
    return RAGFlowQueryClient(api_key=require_ragflow_api_key())


def _error(
    status_code: int, code: str, message: str, request_id: str
) -> JSONResponse:
    from enterprise.gateway.app import ERROR_CODES, ErrorResponse

    retryable = ERROR_CODES.get(code, (500, False))[1]
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            requestId=request_id,
            retryable=retryable,
        ).model_dump(),
    )


class CreateConversationRequest(BaseModel):
    equipmentId: str | None = Field(default=None, max_length=128)
    fixedAssetNo: str | None = Field(default=None, max_length=128)
    faultCode: str | None = Field(default=None, max_length=128)


class ConversationCreated(BaseModel):
    conversationId: str
    createdAt: str
    status: str = "active"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    equipmentId: str | None = Field(default=None, max_length=128)
    faultCode: str | None = Field(default=None, max_length=128)


class CitationOut(BaseModel):
    citationId: str
    sourceType: str = "document"
    title: str
    documentId: str | None = None
    versionId: str | None = None
    pageNo: int | None = None
    bbox: dict | None = None
    assetId: str | None = None
    excerpt: str | None = None
    recordType: str | None = None
    recordId: str | None = None
    chunkId: str | None = None
    imageId: str | None = None
    positions: list[dict] = Field(default_factory=list)
    evidence: dict | None = None


class MessageOut(BaseModel):
    messageId: str
    role: str
    content: str
    status: Literal["completed", "no_reliable_evidence", "failed"]
    citations: list[CitationOut] = Field(default_factory=list)
    createdAt: str


class ConversationDetail(BaseModel):
    conversationId: str
    createdAt: str
    status: str
    messages: list[MessageOut] = Field(default_factory=list)


class AskJsonResponse(BaseModel):
    conversationId: str
    messageId: str
    answer: str
    status: Literal["completed", "no_reliable_evidence", "failed"]
    citations: list[CitationOut] = Field(default_factory=list)


class _FormalQueryError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


async def _resolve_formal_asset(
    db,
    principal: UserPrincipal,
    *,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    existing_context: bool = False,
) -> ResolvedAsset:
    """Resolve a formal conversation identity through the Asset Registry."""
    try:
        resolved = await resolve_asset(
            db,
            tenant_id=principal.tenant_id,
            equipment_id=equipment_id,
            fixed_asset_no=fixed_asset_no,
        )
        if resolved.tenant_id != principal.tenant_id:
            raise AssetRegistryConflict("Asset Registry tenant mismatch")
        for supplied, canonical in (
            (equipment_id, resolved.equipment_id),
            (fixed_asset_no, resolved.fixed_asset_no),
        ):
            if supplied is not None and supplied != canonical:
                raise AssetRegistryConflict("Asset identifiers do not agree")
        return resolved
    except AssetRegistryConflict:
        code = (
            "CONVERSATION_CONTEXT_STALE"
            if existing_context
            else "CONVERSATION_CONTEXT_CONFLICT"
        )
        message = (
            "Conversation context no longer matches the Asset Registry"
            if existing_context
            else "Equipment identifiers do not resolve to the same Asset Registry identity"
        )
        raise _FormalQueryError(code, 409, message) from None
    except AssetRegistryInvalid:
        code = (
            "CONVERSATION_CONTEXT_STALE"
            if existing_context
            else "CONVERSATION_CONTEXT_INVALID"
        )
        message = (
            "Conversation context no longer resolves in the Asset Registry"
            if existing_context
            else "Equipment identifier was not found in the Asset Registry"
        )
        raise _FormalQueryError(
            code, 409 if existing_context else 422, message
        ) from None
    except AssetRegistryUnavailable:
        raise _FormalQueryError(
            "ASSET_REGISTRY_UNAVAILABLE",
            503,
            "Asset Registry is temporarily unavailable",
        ) from None
    except AssetRegistryError:
        raise _FormalQueryError(
            "ASSET_REGISTRY_UNAVAILABLE",
            503,
            "Asset Registry is temporarily unavailable",
        ) from None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()


def _chunk_regions(chunk: dict) -> list[dict]:
    positions = chunk.get("positions") or chunk.get("position_int") or []
    regions: list[dict] = []
    if isinstance(positions, list):
        for pos in positions:
            if not isinstance(pos, (list, tuple)) or len(pos) < 5:
                continue
            try:
                page_no = int(pos[0])
                left = float(pos[1])
                right = float(pos[2])
                top = float(pos[3])
                bottom = float(pos[4])
            except (TypeError, ValueError):
                continue
            if page_no < 1 or left > right or top > bottom:
                continue
            regions.append(
                {
                    "pageNo": page_no,
                    "bbox": {
                        "x1": left,
                        "y1": top,
                        "x2": right,
                        "y2": bottom,
                    },
                }
            )
    return regions


def _chunk_positions(chunk: dict) -> tuple[int | None, dict | None]:
    regions = _chunk_regions(chunk)
    if not regions:
        return None, None
    bbox = dict(regions[0]["bbox"])
    # ``bbox_json`` already persists arbitrary JSON, so nesting all source
    # regions here keeps old snapshots traceable without a schema migration.
    bbox["regions"] = regions
    page_no = regions[0]["pageNo"]
    return page_no, bbox


def _snapshot_regions(citation: dict) -> list[dict]:
    bbox = citation.get("bbox")
    if not isinstance(bbox, dict):
        return []
    regions = bbox.get("regions")
    if isinstance(regions, list):
        return [region for region in regions if isinstance(region, dict)]
    page_no = citation.get("pageNo")
    if page_no is None or not all(
        key in bbox for key in ("x1", "y1", "x2", "y2")
    ):
        return []
    return [
        {
            "pageNo": page_no,
            "bbox": {key: bbox[key] for key in ("x1", "y1", "x2", "y2")},
        }
    ]


def _citation_snapshot_out(citation: dict) -> dict:
    result = dict(citation)
    positions = citation.get("positions") or _snapshot_regions(citation)
    chunk_id = citation.get("chunkId")
    result["positions"] = positions
    result["evidence"] = citation.get("evidence") or {
        "kind": "document_chunk",
        "documentId": citation.get("documentId"),
        "versionId": citation.get("versionId"),
        "chunkId": chunk_id,
        "imageId": citation.get("imageId"),
        "positions": positions,
    }
    return result


def _chunk_to_citation(
    chunk: dict,
    index: int,
    doc: ExtDocumentMap,
    message_id: str | None = None,
) -> dict:
    page_no, bbox = _chunk_positions(chunk)
    regions = _chunk_regions(chunk)
    chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")
    image_id = str(chunk.get("image_id") or chunk.get("img_id") or "") or None
    citation_id = chunk_id or f"chunk-{index}"
    if message_id:
        citation_id = f"{citation_id}-{message_id[:8]}"
    return {
        "citationId": citation_id,
        "sourceType": "document",
        "title": str(
            chunk.get("document_name")
            or chunk.get("docnm_kwd")
            or doc.file_name
            or "PDF document"
        ),
        "documentId": doc.external_document_id,
        "ragflowDocumentId": doc.ragflow_document_id,
        "chunkId": chunk_id,
        "versionId": doc.source_version_id,
        "assetId": doc.asset_id,
        "pageNo": page_no,
        "bbox": bbox,
        "excerpt": chunk.get("content") or chunk.get("content_with_weight"),
        "imageId": image_id,
        "positions": regions,
        "evidence": {
            "kind": "document_chunk",
            "documentId": doc.external_document_id,
            "versionId": doc.source_version_id,
            "chunkId": chunk_id or None,
            "imageId": image_id,
            "positions": regions,
        },
    }


def _resolve_run_outcome(
    completion: dict | None,
    answer: str,
    raw_chunks: list[dict],
) -> str:
    """Resolve message business status from the run result, not citations."""
    if completion is not None:
        data = completion.get("data", {}) if isinstance(completion, dict) else {}
        if isinstance(data, dict):
            explicit = data.get("status")
            if explicit in ("completed", "no_reliable_evidence"):
                return explicit
    if not answer.strip() or not raw_chunks:
        return "no_reliable_evidence"
    return "completed"


class FormalScopeResolver(ScopeResolver):
    """Resolve the authorized retrieval scope from document ACL facts."""

    def __init__(
        self,
        db,
        source_system: str | None = None,
        identity: ResolvedAsset | None = None,
    ):
        self.db = db
        self.source_system = source_system
        self.identity = identity
        self._docs: dict[str, ExtDocumentMap] = {}

    async def resolve(self, context: AclContext) -> AclScope:
        if (
            context is None
            or context.principal is None
            or not context.principal.is_active
        ):
            return AclScope.empty(context.policy_version if context else "")
        source_system = (
            self.source_system
            if self.source_system is not None
            else _query_source_system()
        )
        docs = await list_all_mappings(
            self.db,
            tenant_id=context.principal.tenant_id,
            source_system=source_system,
            statuses=["ready"],
        )
        allowed_docs: list[ExtDocumentMap] = []
        dataset_ids: set[str] = set()
        quality_required = (
            os.environ.get("ENTERPRISE_QUERY_QUALITY_REQUIRED", "true").lower()
            in ("1", "true", "yes", "on")
        )
        if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
            quality_required = (
                os.environ.get("ENTERPRISE_QUERY_QUALITY_REQUIRED", "false").lower()
                in ("1", "true", "yes", "on")
        )
        for doc in docs:
            if not doc.ragflow_dataset_id or not doc.ragflow_document_id:
                continue
            if self.identity is not None and (
                doc.equipment_id != self.identity.equipment_id
                or doc.fixed_asset_no != self.identity.fixed_asset_no
            ):
                # Asset Registry identity is authoritative.  A document with
                # missing or mismatched metadata cannot become an alias.
                continue
            if doc.source_kind == "FILE_SHARE" and not doc.current_version:
                # A new external version may be parsed while the previously
                # promoted version continues serving retrieval traffic.
                continue
            if doc.business_status != "active":
                continue
            if quality_required:
                evaluation = await get_latest_evaluation(
                    self.db,
                    doc.tenant_id,
                    doc.source_system,
                    doc.external_document_id,
                    doc.source_version_id,
                )
                quality_allowed, _ = enforce_quality_gate(evaluation)
                if not quality_allowed:
                    continue
            facts = DocumentAclFacts(
                tenant_id=doc.tenant_id,
                department_id=doc.department_id,
                security_level=doc.security_level,
                business_status=doc.business_status,
                allow_group_ids=_json_list(doc.allow_group_ids),
                deny_group_ids=_json_list(doc.deny_group_ids),
            )
            decision = evaluate_document_acl(context.principal, facts)
            if not decision.allowed:
                continue
            self._docs[doc.ragflow_document_id] = doc
            allowed_docs.append(doc)
            dataset_ids.add(doc.ragflow_dataset_id)
        return AclScope.materialized(
            tuple(dataset_ids),
            tuple(doc.ragflow_document_id for doc in allowed_docs),
            policy_version=context.policy_version,
        )


async def _conversation_lock(conversation_id: str) -> asyncio.Lock:
    lock = _conversation_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[conversation_id] = lock
    return lock


async def _load_conversation(
    db,
    principal: UserPrincipal,
    conversation_id: str,
    request_id: str,
):
    await conversation_store.ensure_schema(db)
    conversation = await conversation_store.get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not conversation:
        return None, _error(
            404,
            "CONVERSATION_NOT_FOUND",
            "Conversation not found",
            request_id,
        )
    return conversation, None


async def _resolve_scope(
    db,
    principal: UserPrincipal,
    request_id: str,
    *,
    identity: ResolvedAsset | None = None,
) -> tuple[AclScope, dict[str, ExtDocumentMap]]:
    context = AclContext(principal=principal)
    resolver = FormalScopeResolver(db, identity=identity)
    scope = await compile_scope(context, resolver)
    return scope, resolver._docs


async def _ensure_formal_context(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: AskRequest,
) -> tuple[dict, ResolvedAsset]:
    """Validate the persisted context before any ACL scope is compiled."""
    stored_equipment = conversation.get("equipment_id")
    stored_fixed = conversation.get("fixed_asset_no")
    if req.equipmentId is not None and stored_equipment is not None:
        if req.equipmentId != stored_equipment:
            raise _FormalQueryError(
                "CONVERSATION_CONTEXT_CONFLICT",
                409,
                "Request equipmentId does not match the conversation context",
            )

    candidate_equipment = stored_equipment or req.equipmentId
    if not candidate_equipment and not stored_fixed:
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_REQUIRED",
            422,
            "A canonical equipment context is required before sending a message",
        )
    resolved = await _resolve_formal_asset(
        db,
        principal,
        equipment_id=candidate_equipment,
        fixed_asset_no=stored_fixed,
        existing_context=bool(stored_equipment or stored_fixed),
    )
    if (
        stored_equipment is not None
        and resolved.equipment_id != stored_equipment
    ) or (
        stored_fixed is not None
        and resolved.fixed_asset_no != stored_fixed
    ):
        raise _FormalQueryError(
            "CONVERSATION_CONTEXT_STALE",
            409,
            "Conversation context no longer matches the Asset Registry",
        )

    if stored_equipment is None or stored_fixed is None:
        await db.execute(
            """UPDATE ext_conversation
               SET equipment_id=?, fixed_asset_no=?
               WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
            (
                resolved.equipment_id,
                resolved.fixed_asset_no,
                conversation["conversation_id"],
                principal.tenant_id,
                principal.business_user_id,
            ),
        )
        await db.commit()
        conversation = {
            **conversation,
            "equipment_id": resolved.equipment_id,
            "fixed_asset_no": resolved.fixed_asset_no,
        }
    return conversation, resolved


async def _ensure_chat(
    client,
    principal: UserPrincipal,
    scope: AclScope,
) -> str:
    chat_name = f"enterprise-formal-{principal.tenant_id}"
    chats = await client.list_chats(name=chat_name)
    chat_id = next(
        (c.get("id") for c in chats if c.get("name") == chat_name),
        None,
    )
    prompt_config = enterprise_prompt_config_for_api()
    if not chat_id:
        created = await client.create_chat(
            chat_name,
            list(scope.dataset_ids),
            prompt_config=prompt_config,
        )
        chat_id = (created.get("data") or {}).get("id", "")
        if not chat_id:
            raise RAGFlowAPIError("Chat id missing after create", 502)
        return chat_id
    chat = next((c for c in chats if c.get("id") == chat_id), {})
    existing_datasets = set(chat.get("dataset_ids") or [])
    needs_datasets = not set(scope.dataset_ids).issubset(existing_datasets)
    needs_prompt = needs_enterprise_prompt_upgrade(chat)
    if needs_datasets or needs_prompt:
        await client.update_chat(
            chat_id,
            list(scope.dataset_ids) if needs_datasets else None,
            prompt_config=prompt_config if needs_prompt else None,
        )
    return chat_id


def _validate_chunks(
    chunks: list[dict],
    docs_by_ragflow: dict[str, ExtDocumentMap],
) -> None:
    out_of_scope = [
        c for c in chunks if c.get("document_id") not in docs_by_ragflow
    ]
    if out_of_scope:
        logger.warning(
            "RAGFlow returned out-of-scope chunks document_ids=%s",
            sorted({c.get("document_id") for c in out_of_scope}),
        )
        raise _FormalQueryError(
            "RAGFLOW_SCOPE_VIOLATION",
            502,
            "RAGFlow retrieval returned an out-of-scope document",
        )


def _build_citations(
    chunks: list[dict],
    docs_by_ragflow: dict[str, ExtDocumentMap],
    message_id: str | None = None,
) -> list[dict]:
    _validate_chunks(chunks, docs_by_ragflow)
    return [
        _chunk_to_citation(
            c, index, docs_by_ragflow[c["document_id"]], message_id
        )
        for index, c in enumerate(chunks)
        if isinstance(c, dict)
    ]


async def _persist_pair(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    question: str,
    answer: str,
    status: str,
    citations: list[dict],
    ragflow_message_id: str | None,
    user_message_id: str,
    assistant_message_id: str,
) -> None:
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        message_id=user_message_id,
        role="user",
        status="completed",
        content=question,
        citations=[],
        ragflow_message_id=ragflow_message_id,
    )
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        message_id=assistant_message_id,
        role="assistant",
        status=status,
        content=answer,
        citations=citations,
        ragflow_message_id=ragflow_message_id,
    )


async def _persist_assistant(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    message_id: str,
    answer: str,
    status: str,
    citations: list[dict],
    ragflow_message_id: str | None,
) -> None:
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        message_id=message_id,
        role="assistant",
        status=status,
        content=answer,
        citations=citations,
        ragflow_message_id=ragflow_message_id,
    )


async def _run_ask(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: AskRequest,
    scope: AclScope,
    docs_by_ragflow: dict[str, ExtDocumentMap],
    request_id: str,
) -> AskJsonResponse:
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())
    await conversation_store.add_message(
        db,
        conversation_id=conversation["conversation_id"],
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        message_id=user_message_id,
        role="user",
        status="completed",
        content=req.question,
        citations=[],
    )
    client = _query_client()
    try:
        chat_id = await _ensure_chat(client, principal, scope)
        completion = await client.chat_completion(
            chat_id,
            req.question,
            session_id=conversation.get("ragflow_session_id"),
            doc_ids=list(scope.document_ids),
        )
    except (RAGFlowAPIError, _FormalQueryError) as e:
        if isinstance(e, RAGFlowAPIError):
            code = (
                "RAGFLOW_API_INCOMPATIBLE"
                if e.status_code and 400 <= e.status_code < 500
                else "RAGFLOW_UNAVAILABLE"
            )
            status_code = (
                503 if code == "RAGFLOW_UNAVAILABLE" else 502
            )
            message = "RAGFlow service error"
        else:
            code = e.code
            status_code = e.status_code
            message = e.message
        await conversation_store.add_message(
            db,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            role="assistant",
            status="failed",
            content="",
            citations=[],
        )
        logger.warning(
            "Formal ask RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        raise _FormalQueryError(code, status_code, message)

    try:
        data = completion.get("data", {}) if isinstance(completion, dict) else {}
        reference = data.get("reference", {}) if isinstance(data, dict) else {}
        raw_chunks = (
            reference.get("chunks", [])
            if isinstance(reference, dict)
            else []
        )
        chunks = [c for c in raw_chunks if isinstance(c, dict)]
        citations = _build_citations(
            chunks, docs_by_ragflow, assistant_message_id
        )
        answer = data.get("answer", "") if isinstance(data, dict) else ""
        status = _resolve_run_outcome(completion, answer, chunks)
        if status == "no_reliable_evidence":
            answer = NO_RELIABLE_EVIDENCE_ANSWER
            citations = []
        ragflow_session_id = (
            data.get("session_id") if isinstance(data, dict) else None
        )
        ragflow_message_id = (
            data.get("id") if isinstance(data, dict) else None
        )
        if chat_id:
            await conversation_store.update_conversation_mapping(
                db,
                conversation_id=conversation["conversation_id"],
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                ragflow_chat_id=chat_id,
                ragflow_session_id=ragflow_session_id,
            )
        await conversation_store.add_message(
            db,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            role="assistant",
            status=status,
            content=answer,
            citations=citations,
            ragflow_message_id=ragflow_message_id,
        )
    except _FormalQueryError as e:
        await conversation_store.add_message(
            db,
            conversation_id=conversation["conversation_id"],
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            role="assistant",
            status="failed",
            content="",
            citations=[],
        )
        raise
    return AskJsonResponse(
        conversationId=conversation["conversation_id"],
        messageId=assistant_message_id,
        answer=answer,
        status=status,
        citations=[CitationOut(**c) for c in citations],
    )


async def _stream_ask_events(
    db,
    principal: UserPrincipal,
    conversation: dict,
    req: AskRequest,
    scope: AclScope,
    docs_by_ragflow: dict[str, ExtDocumentMap],
    request_id: str,
) -> AsyncIterator[str]:
    conversation_id = conversation["conversation_id"]
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())
    yield _sse(
        "run.started",
        {
            "conversationId": conversation_id,
            "requestId": request_id,
        },
    )
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        message_id=user_message_id,
        role="user",
        status="completed",
        content=req.question,
        citations=[],
    )
    accumulated = ""
    chunks: list[dict] = []
    ragflow_message_id: str | None = None
    ragflow_session_id = conversation.get("ragflow_session_id")
    status = "failed"
    answer = ""
    citations: list[dict] = []
    chat_id: str | None = None
    try:
        client = _query_client()
        chat_id = await _ensure_chat(client, principal, scope)
        async for payload in client.chat_completion_stream(
            chat_id,
            req.question,
            session_id=ragflow_session_id,
            doc_ids=list(scope.document_ids),
        ):
            data = payload.get("data") if isinstance(payload, dict) else None
            if data is True:
                break
            if not isinstance(data, dict):
                continue
            if data.get("session_id"):
                ragflow_session_id = data["session_id"]
            if data.get("id"):
                ragflow_message_id = data["id"]
            reference = data.get("reference") or {}
            raw_chunks = (
                reference.get("chunks", [])
                if isinstance(reference, dict)
                else []
            )
            chunks.extend(c for c in raw_chunks if isinstance(c, dict))
            delta = data.get("answer")
            is_final = bool(data.get("final"))
            if is_final and not accumulated and delta:
                accumulated = delta
            elif not is_final and delta:
                accumulated += delta
                yield _sse(
                    "answer.delta",
                    {
                        "conversationId": conversation_id,
                        "content": delta,
                    },
                )
        citations = _build_citations(
            chunks, docs_by_ragflow, assistant_message_id
        )
        status = _resolve_run_outcome(None, accumulated, chunks)
        if status == "no_reliable_evidence":
            answer = NO_RELIABLE_EVIDENCE_ANSWER
            citations = []
        else:
            answer = accumulated
        if chat_id:
            await conversation_store.update_conversation_mapping(
                db,
                conversation_id=conversation_id,
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                ragflow_chat_id=chat_id,
                ragflow_session_id=ragflow_session_id,
            )
        await _persist_assistant(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            answer=answer,
            status=status,
            citations=citations,
            ragflow_message_id=ragflow_message_id,
        )
        if status == "completed":
            for citation in citations:
                yield _sse("citation", citation)
        yield _sse(
            "answer.completed",
            {
                "conversationId": conversation_id,
                "messageId": assistant_message_id,
                "status": status,
                "citations": citations,
            },
        )
    except asyncio.CancelledError:
        await _persist_assistant(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            answer=accumulated,
            status="failed",
            citations=[],
            ragflow_message_id=ragflow_message_id,
        )
        raise
    except _FormalQueryError as e:
        await _persist_assistant(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            answer=accumulated,
            status="failed",
            citations=[],
            ragflow_message_id=ragflow_message_id,
        )
        yield _sse(
            "run.failed",
            {
                "conversationId": conversation_id,
                "code": e.code,
                "message": e.message,
            },
        )
    except RAGFlowAPIError as e:
        code = (
            "RAGFLOW_API_INCOMPATIBLE"
            if e.status_code and 400 <= e.status_code < 500
            else "RAGFLOW_UNAVAILABLE"
        )
        logger.warning(
            "Formal stream RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        await _persist_assistant(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
            message_id=assistant_message_id,
            answer=accumulated,
            status="failed",
            citations=[],
            ragflow_message_id=ragflow_message_id,
        )
        yield _sse(
            "run.failed",
            {
                "conversationId": conversation_id,
                "code": code,
                "message": "RAGFlow service error",
            },
        )


@router.post(
    "/conversations",
    response_model=ConversationCreated,
    status_code=201,
)
async def create_conversation(
    req: CreateConversationRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_capability("list_sessions")),
):
    request_id = str(uuid.uuid4())
    await conversation_store.ensure_schema(db)
    try:
        canonical = None
        if req.equipmentId is not None or req.fixedAssetNo is not None:
            canonical = await _resolve_formal_asset(
                db,
                principal,
                equipment_id=req.equipmentId,
                fixed_asset_no=req.fixedAssetNo,
            )
    except _FormalQueryError as error:
        return _error(error.status_code, error.code, error.message, request_id)
    conversation_id = str(uuid.uuid4())
    conversation = await conversation_store.create_conversation(
        db,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        conversation_id=conversation_id,
        equipment_id=canonical.equipment_id if canonical else None,
        fixed_asset_no=canonical.fixed_asset_no if canonical else None,
        current_fault_code=req.faultCode,
    )
    return ConversationCreated(
        conversationId=conversation["conversation_id"],
        createdAt=conversation["created_at"],
        status=conversation["status"],
    )


@router.post(
    "/conversations/{conversation_id}/messages:stream",
    include_in_schema=True,
)
async def ask_message(
    conversation_id: str,
    request: Request,
    req: AskRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("ask", "view_citations")
    ),
):
    request_id = str(uuid.uuid4())
    conversation, error = await _load_conversation(
        db, principal, conversation_id, request_id
    )
    if error:
        return error
    lock = await _conversation_lock(conversation_id)
    async with lock:
        conversation, error = await _load_conversation(
            db, principal, conversation_id, request_id
        )
        if error:
            return error
        try:
            conversation, identity = await _ensure_formal_context(
                db, principal, conversation, req
            )
        except _FormalQueryError as e:
            return _error(e.status_code, e.code, e.message, request_id)
        scope, docs_by_ragflow = await _resolve_scope(
            db, principal, request_id, identity=identity
        )
        if scope.is_empty:
            message_id = str(uuid.uuid4())
            await _persist_pair(
                db,
                conversation_id=conversation_id,
                tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id,
                question=req.question,
                answer=NO_RELIABLE_EVIDENCE_ANSWER,
                status="no_reliable_evidence",
                citations=[],
                ragflow_message_id=None,
                user_message_id=str(uuid.uuid4()),
                assistant_message_id=message_id,
            )
            if request.headers.get("accept", "").lower().find(
                "text/event-stream"
            ) >= 0 or request.query_params.get("stream", "").lower() in (
                "1",
                "true",
            ):
                return StreamingResponse(
                    _empty_evidence_stream(
                        conversation_id, message_id, request_id
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            return AskJsonResponse(
                conversationId=conversation_id,
                messageId=message_id,
                answer=NO_RELIABLE_EVIDENCE_ANSWER,
                status="no_reliable_evidence",
                citations=[],
            )
        if request.headers.get("accept", "").lower().find(
            "text/event-stream"
        ) >= 0 or request.query_params.get("stream", "").lower() in (
            "1",
            "true",
        ):
            return StreamingResponse(
                _stream_ask_events(
                    db,
                    principal,
                    conversation,
                    req,
                    scope,
                    docs_by_ragflow,
                    request_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            result = await _run_ask(
                db,
                principal,
                conversation,
                req,
                scope,
                docs_by_ragflow,
                request_id,
            )
        except _FormalQueryError as e:
            return _error(e.status_code, e.code, e.message, request_id)
        return result


async def _empty_evidence_stream(
    conversation_id: str,
    message_id: str,
    request_id: str,
) -> AsyncIterator[str]:
    yield _sse(
        "run.started",
        {"conversationId": conversation_id, "requestId": request_id},
    )
    yield _sse(
        "answer.completed",
        {
            "conversationId": conversation_id,
            "messageId": message_id,
            "status": "no_reliable_evidence",
            "citations": [],
        },
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
)
async def get_conversation(
    conversation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("list_sessions", "view_citations")
    ),
):
    request_id = str(uuid.uuid4())
    conversation, error = await _load_conversation(
        db, principal, conversation_id, request_id
    )
    if error:
        return error
    messages = await conversation_store.list_messages(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    return ConversationDetail(
        conversationId=conversation["conversation_id"],
        createdAt=conversation["created_at"],
        status=conversation["status"],
        messages=[
            MessageOut(
                **{
                    **message,
                    "citations": [
                        _citation_snapshot_out(citation)
                        for citation in message.get("citations", [])
                    ],
                }
            )
            for message in messages
        ],
    )


@router.get("/citations/{citation_id}", response_model=CitationOut)
async def get_citation(
    citation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("view_citations", "list_sessions")
    ),
):
    request_id = str(uuid.uuid4())
    await conversation_store.ensure_schema(db)
    citation = await conversation_store.get_citation(
        db,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not citation:
        return _error(
            404, "CITATION_NOT_FOUND", "Citation not found", request_id
        )
    doc = await _citation_document_for_principal(db, principal, citation)
    if doc is None:
        return _error(403, "ACL_DENIED", "Access denied", request_id)
    chunk_evidence: dict = {}
    chunk_id = citation.get("chunkId")
    if (
        chunk_id
        and doc.ragflow_dataset_id
        and doc.ragflow_document_id
    ):
        try:
            client = _query_client()
            chunk_evidence = await client.get_chunk_evidence(
                doc.ragflow_dataset_id,
                doc.ragflow_document_id,
                chunk_id,
                request_id=request_id,
            )
        except (RAGFlowAPIError, RuntimeError):
            # The immutable citation snapshot remains readable even when the
            # upstream evidence service is temporarily unavailable.
            logger.warning(
                "Citation evidence refresh unavailable citation_id=%s",
                citation_id,
            )
    positions = (
        _chunk_regions(chunk_evidence)
        or citation.get("positions")
        or _snapshot_regions(citation)
    )
    image_id = str(
        chunk_evidence.get("image_id") or citation.get("imageId") or ""
    ) or None
    return CitationOut(
        citationId=citation["citationId"],
        sourceType=citation["sourceType"],
        title=citation["title"],
        documentId=citation["documentId"],
        versionId=citation["versionId"],
        pageNo=citation["pageNo"],
        bbox=citation["bbox"],
        assetId=citation["assetId"],
        excerpt=citation["excerpt"],
        recordType=citation["recordType"],
        recordId=citation["recordId"],
        chunkId=chunk_id,
        imageId=image_id,
        positions=positions,
        evidence=citation.get("evidence") or {
            "kind": "document_chunk",
            "documentId": citation.get("documentId"),
            "versionId": citation.get("versionId"),
            "chunkId": chunk_id,
            "imageId": image_id,
            "positions": positions,
        },
    )


@router.get("/citations/{citation_id}/source", include_in_schema=True)
async def get_citation_source(
    citation_id: str,
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(
        require_capability("view_citations", "list_sessions")
    ),
):
    """Stream only the exact ACL-authorized FILE_SHARE source version."""
    await conversation_store.ensure_schema(db)
    citation = await conversation_store.get_citation(
        db,
        citation_id=citation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    doc = await _citation_document_for_principal(db, principal, citation) if citation else None
    # Missing and unauthorized citations deliberately share the same external
    # result so the source endpoint cannot be used for enumeration.
    if doc is None:
        return JSONResponse(status_code=404, content={"code": "CITATION_NOT_FOUND"})
    return await source_response(request, doc)


async def _citation_document_for_principal(
    db,
    principal: UserPrincipal,
    citation: dict,
) -> ExtDocumentMap | None:
    ragflow_document_id = citation.get("ragflowDocumentId")
    external_document_id = citation.get("documentId") or citation.get(
        "externalDocumentId"
    )
    source_version_id = citation.get("versionId") or citation.get("sourceVersionId")
    source_system = _query_source_system()
    doc = None
    if external_document_id and source_system:
        versions = await get_versions_for_document(
            db,
            principal.tenant_id,
            source_system,
            external_document_id,
        )
        for candidate in versions:
            if source_version_id and candidate.source_version_id != source_version_id:
                continue
            if (
                ragflow_document_id
                and candidate.ragflow_document_id == ragflow_document_id
            ):
                doc = candidate
                break
            if not ragflow_document_id and source_version_id:
                doc = candidate
                break
            if not ragflow_document_id and candidate.current_version:
                doc = candidate
                break
    if doc is None:
        docs = await list_all_mappings(
            db,
            tenant_id=principal.tenant_id,
            source_system=source_system,
        )
        for candidate in docs:
            if source_version_id and candidate.source_version_id != source_version_id:
                continue
            if (
                ragflow_document_id
                and candidate.ragflow_document_id == ragflow_document_id
            ):
                doc = candidate
                break
            if (
                external_document_id
                and candidate.external_document_id == external_document_id
            ):
                doc = candidate
                break
    if doc is None:
        return None
    if doc.business_status not in {"active", "superseded"}:
        return None
    facts = DocumentAclFacts(
        tenant_id=doc.tenant_id,
        department_id=doc.department_id,
        security_level=doc.security_level,
        # A superseded version remains a readable historical snapshot.  Its
        # ACL facts are still current, while disabled/deleted versions are
        # denied by the status check above.
        business_status="active",
        allow_group_ids=_json_list(doc.allow_group_ids),
        deny_group_ids=_json_list(doc.deny_group_ids),
    )
    return doc if evaluate_document_acl(principal, facts).allowed else None


async def _citation_document_allowed(
    db,
    principal: UserPrincipal,
    citation: dict,
) -> bool:
    return (
        await _citation_document_for_principal(db, principal, citation)
    ) is not None
