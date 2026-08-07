"""Temporary query demo router, now owned by the WP-04 retrieval scope.

Routes keep the legacy ``/enterprise/api/v1/demo`` prefix for compatibility,
but use UserPrincipal for identity and AclScope for resource authorization.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from enterprise.gateway.acl.context import AclContext
from enterprise.gateway.acl.schema import AclScope
from enterprise.gateway.acl.scope import ScopeResolver, compile_scope
from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.config import require_ragflow_api_key
from enterprise.gateway.quality.gate import enforce_quality_gate
from enterprise.gateway.quality.models import get_latest_evaluation
from enterprise.gateway.query import acl_store
from enterprise.gateway.query import conversation_store
from enterprise.gateway.query.ragflow_client import (
    RAGFlowQueryClient,
    RAGFlowQueryStub,
)
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    get_mapping,
    insert_mapping,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import RAGFlowAPIError
from enterprise.gateway.sync.source_adapter import SourceStub
from enterprise.gateway.sync.status_mapping import enterprise_stage
from enterprise.gateway.sync.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/demo", tags=["query-demo"])

_MAX_DEMO_PDF_BYTES = int(
    os.environ.get("ENTERPRISE_DEMO_MAX_PDF_BYTES", str(128 * 1024 * 1024))
)
_query_stub: RAGFlowQueryStub | None = None


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
    from enterprise.gateway.app import ErrorResponse

    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code,
            message=message,
            requestId=request_id,
        ).model_dump(),
    )


class DemoScopeResolver(ScopeResolver):
    """Resolve the authorized scope to the caller's own document mapping."""

    def __init__(
        self,
        db,
        external_document_id: str,
        source_system: str = "DEMO",
        source_version_id: str = "v1",
    ) -> None:
        self.db = db
        self.external_document_id = external_document_id
        self.source_system = source_system
        self.source_version_id = source_version_id

    async def resolve(self, context: AclContext) -> AclScope:
        doc = await get_mapping(
            self.db,
            context.principal.tenant_id,
            self.source_system,
            self.external_document_id,
            self.source_version_id,
        )
        if (
            not doc
            or not doc.ragflow_dataset_id
            or not doc.ragflow_document_id
            or doc.business_status != "active"
        ):
            return AclScope.empty(context.policy_version)
        allowed = await acl_store.is_allowed(
            self.db,
            tenant_id=context.principal.tenant_id,
            external_document_id=self.external_document_id,
            business_user_id=context.principal.business_user_id,
        )
        if not allowed:
            return AclScope.empty(context.policy_version)
        return AclScope.materialized(
            (doc.ragflow_dataset_id,),
            (doc.ragflow_document_id,),
            policy_version=context.policy_version,
        )


async def _resolve_authorized_document(
    db,
    principal: UserPrincipal,
    external_document_id: str,
    request_id: str,
):
    doc = await get_mapping(
        db,
        principal.tenant_id,
        "DEMO",
        external_document_id,
        "v1",
    )
    if not doc:
        return None, None, None
    context = AclContext(principal=principal)
    await acl_store.ensure_schema(db)
    scope = await compile_scope(
        context,
        DemoScopeResolver(db, external_document_id),
    )
    if scope.is_empty:
        return None, None, _error(
            403, "ACL_DENIED", "Access denied", request_id
        )
    return doc, scope, None


async def _authorized_document_for_gate(
    db,
    principal: UserPrincipal,
    external_document_id: str,
    request_id: str,
):
    """Authorize a document for quality-gate checks without materializing a
    retrieval scope (failed documents have no RAGFlow document id yet)."""
    doc = await get_mapping(
        db,
        principal.tenant_id,
        "DEMO",
        external_document_id,
        "v1",
    )
    if not doc:
        return None, _error(404, "DOCUMENT_NOT_FOUND", "Document not found", request_id)
    await acl_store.ensure_schema(db)
    allowed = await acl_store.is_allowed(
        db,
        tenant_id=principal.tenant_id,
        external_document_id=external_document_id,
        business_user_id=principal.business_user_id,
    )
    if not allowed:
        return None, _error(403, "ACL_DENIED", "Access denied", request_id)
    return doc, None


class DemoDocumentResponse(BaseModel):
    externalDocumentId: str
    sourceVersionId: str
    ragflowDatasetId: str | None = None
    ragflowDocumentId: str | None = None
    status: str
    stage: str | None = None
    deduplicated: bool = False


class DemoAskRequest(BaseModel):
    externalDocumentId: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=8000)
    conversationId: str | None = None


class DemoCitation(BaseModel):
    citationId: str
    sourceType: str
    title: str
    documentId: str | None = None
    versionId: str | None = None
    pageNo: int | None = None
    bbox: dict | None = None
    assetId: str | None = None
    excerpt: str | None = None
    recordType: str | None = None
    recordId: str | None = None


class DemoAskResponse(BaseModel):
    answer: str
    citations: list[DemoCitation]
    conversationId: str
    ragflowSessionId: str | None = None


def _to_citation(chunk: dict, index: int) -> DemoCitation:
    positions = chunk.get("positions") or chunk.get("position_int") or []
    page_no: int | None = None
    bbox: dict | None = None
    if isinstance(positions, list):
        for pos in positions:
            if not isinstance(pos, list) or not pos:
                continue
            try:
                page_no = int(pos[0])
            except (TypeError, ValueError):
                page_no = None
            if len(pos) >= 5:
                try:
                    bbox = {
                        "x1": float(pos[1]),
                        "y1": float(pos[2]),
                        "x2": float(pos[3]),
                        "y2": float(pos[4]),
                    }
                except (TypeError, ValueError):
                    bbox = None
            break
    return DemoCitation(
        citationId=str(
            chunk.get("id") or chunk.get("chunk_id") or f"chunk-{index}"
        ),
        sourceType="document",
        title=str(
            chunk.get("document_name")
            or chunk.get("docnm_kwd")
            or "PDF document"
        ),
        documentId=chunk.get("document_id") or chunk.get("doc_id"),
        pageNo=page_no,
        bbox=bbox,
        excerpt=chunk.get("content") or chunk.get("content_with_weight"),
    )


def _filter_citations(
    citations: list[DemoCitation], scope: AclScope
) -> list[DemoCitation]:
    allowed = set(scope.document_ids)
    return [
        c for c in citations
        if c.documentId and c.documentId in allowed
    ]


@router.post("/documents")
async def upload_document(
    request: Request,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_user_principal),
):
    request_id = str(uuid.uuid4())
    external_document_id = request.query_params.get(
        "externalDocumentId", ""
    ).strip()
    file_name = request.headers.get("X-File-Name", "").strip()

    if not external_document_id or len(external_document_id) > 128:
        return _error(
            422,
            "VALIDATION_ERROR",
            "externalDocumentId is required and must be <= 128 chars",
            request_id,
        )

    content = await request.body()
    if not content:
        return _error(
            422, "VALIDATION_ERROR", "PDF body is empty", request_id
        )
    if len(content) > _MAX_DEMO_PDF_BYTES:
        return _error(
            413,
            "VALIDATION_ERROR",
            "PDF body exceeds configured size limit",
            request_id,
        )
    content_type = request.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("application/pdf"):
        return _error(
            422,
            "VALIDATION_ERROR",
            "Content-Type must be application/pdf",
            request_id,
        )
    if not file_name:
        file_name = f"{external_document_id}.pdf"
    if len(file_name) > 255:
        return _error(
            422,
            "VALIDATION_ERROR",
            "X-File-Name must be <= 255 chars",
            request_id,
        )

    tenant_id = principal.tenant_id
    sha256 = hashlib.sha256(content).hexdigest()
    existing = await get_mapping(
        db, tenant_id, "DEMO", external_document_id, "v1"
    )
    if existing:
        return DemoDocumentResponse(
            externalDocumentId=existing.external_document_id,
            sourceVersionId=existing.source_version_id,
            ragflowDatasetId=existing.ragflow_dataset_id,
            ragflowDocumentId=existing.ragflow_document_id,
            status=existing.sync_status,
            stage=enterprise_stage(existing.sync_status),
            deduplicated=True,
        )

    doc = ExtDocumentMap(
        tenant_id=tenant_id,
        source_system="DEMO",
        external_document_id=external_document_id,
        source_version_id="v1",
        event_id=str(uuid.uuid4()),
        sha256=sha256,
        file_name=file_name,
        media_type="application/pdf",
        sync_status="received",
    )
    try:
        doc = await insert_mapping(db, doc)
    except Exception:
        logger.exception("Query demo document insert failed")
        return _error(
            500, "INTERNAL_ERROR", "Internal service error", request_id
        )

    await update_mapping_status(db, doc, "validated")
    client = _query_client()
    try:
        datasets = await client.list_datasets()
        dataset_id = next(
            (
                d.get("id")
                for d in datasets
                if d.get("name") == f"enterprise-{tenant_id}"
            ),
            None,
        )
        if not dataset_id:
            created = await client.create_dataset(f"enterprise-{tenant_id}")
            dataset_id = (created.get("data") or {}).get("id", "")
        if not dataset_id:
            raise RAGFlowAPIError("Dataset id missing after create", 502)

        upload_result = await client.upload_document(
            dataset_id, file_name, content
        )
        docs_data = (
            upload_result.get("data", [])
            if isinstance(upload_result, dict)
            else []
        )
        if not docs_data:
            await update_mapping_status(
                db,
                doc,
                "failed",
                error_code="DOCUMENT_SYNC_FAILED",
                error_message="RAGFlow upload returned no document",
            )
            return _error(
                502,
                "DOCUMENT_SYNC_FAILED",
                "Document synchronization failed",
                request_id,
            )

        ragflow_doc = docs_data[0]
        doc.ragflow_dataset_id = dataset_id
        doc.ragflow_document_id = ragflow_doc.get("id", "")
        doc.ragflow_task_id = doc.ragflow_document_id
        await update_mapping_status(
            db,
            doc,
            "registered",
            pipeline_status=ragflow_doc.get("run", "UNSTART"),
        )
        await client.start_parsing(dataset_id, [doc.ragflow_document_id])
        await update_mapping_status(
            db, doc, "parsing", pipeline_status="RUNNING"
        )
        await acl_store.grant(
            db,
            tenant_id=tenant_id,
            external_document_id=external_document_id,
            business_user_id=principal.business_user_id,
        )
    except RAGFlowAPIError as e:
        code = (
            "RAGFLOW_API_INCOMPATIBLE"
            if e.status_code and 400 <= e.status_code < 500
            else "RAGFLOW_UNAVAILABLE"
        )
        await update_mapping_status(
            db,
            doc,
            "failed",
            error_code=code,
            error_message="RAGFlow API error",
        )
        logger.warning(
            "Query demo RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        return _error(
            503 if code == "RAGFLOW_UNAVAILABLE" else 502,
            code,
            "RAGFlow service error",
            request_id,
        )

    return DemoDocumentResponse(
        externalDocumentId=external_document_id,
        sourceVersionId="v1",
        ragflowDatasetId=dataset_id,
        ragflowDocumentId=doc.ragflow_document_id,
        status="parsing",
        stage="parsing",
    )


@router.get("/documents/{external_document_id}/status")
async def get_document_status(
    external_document_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_user_principal),
):
    request_id = str(uuid.uuid4())
    doc, _scope, acl_error = await _resolve_authorized_document(
        db, principal, external_document_id, request_id
    )
    if acl_error:
        return acl_error
    if doc is None:
        return _error(
            404, "DOCUMENT_NOT_FOUND", "Document not found", request_id
        )

    service = SyncService(db, SourceStub(), _query_client())
    try:
        doc = await service.refresh_status(doc)
    except Exception:
        logger.warning(
            "Query demo status refresh failed for document %s",
            external_document_id,
        )

    return DemoDocumentResponse(
        externalDocumentId=doc.external_document_id,
        sourceVersionId=doc.source_version_id,
        ragflowDatasetId=doc.ragflow_dataset_id,
        ragflowDocumentId=doc.ragflow_document_id,
        status=doc.sync_status,
        stage=enterprise_stage(doc.sync_status),
    )


@router.post("/ask", response_model=DemoAskResponse)
async def ask(
    req: DemoAskRequest,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_user_principal),
):
    request_id = str(uuid.uuid4())
    doc, acl_error = await _authorized_document_for_gate(
        db, principal, req.externalDocumentId, request_id
    )
    if acl_error:
        return acl_error
    if doc is None:
        return _error(404, "DOCUMENT_NOT_FOUND", "Document not found", request_id)
    evaluation = await get_latest_evaluation(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    quality_code = None
    quality_gate_default = (
        "false" if os.environ.get("ENTERPRISE_TEST_MODE") == "1" else "true"
    )
    if os.environ.get(
        "ENTERPRISE_QUALITY_GATE_ENABLED", quality_gate_default
    ).lower() == "true":
        strict_mode = (
            os.environ.get("ENTERPRISE_QUALITY_STRICT_MODE", "true").lower()
            == "true"
        )
        demo_warn_mode = (
            os.environ.get("ENTERPRISE_QUALITY_DEMO_WARN_MODE", "false").lower()
            == "true"
        )
        quality_allowed, quality_code = enforce_quality_gate(
            evaluation,
            strict_mode=strict_mode,
            demo_warn_mode=demo_warn_mode,
        )
        if not quality_allowed:
            return _error(
                409,
                quality_code,
                f"Document quality gate rejected request ({quality_code})",
                request_id,
            )
    if quality_code == "DOCUMENT_QUALITY_WARN":
        logger.warning(
            "Quality warn mode allowed ask for document=%s request_id=%s",
            req.externalDocumentId,
            request_id,
        )
    if doc.sync_status != "ready":
        return _error(
            409,
            "DOCUMENT_NOT_READY",
            f"Document status is {doc.sync_status}, expected ready",
            request_id,
        )

    doc, scope, acl_error = await _resolve_authorized_document(
        db, principal, req.externalDocumentId, request_id
    )
    if acl_error:
        return acl_error
    if doc is None or scope.is_empty:
        return _error(
            403, "ACL_DENIED", "Access denied", request_id
        )

    await conversation_store.ensure_schema(db)
    conversation_id = req.conversationId or str(uuid.uuid4())
    session_id = None
    if req.conversationId:
        conversation = await conversation_store.get_conversation_map(
            db,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            business_user_id=principal.business_user_id,
        )
        if not conversation:
            return _error(
                404,
                "CONVERSATION_NOT_FOUND",
                "Conversation not found",
                request_id,
            )
        if conversation["external_document_id"] != req.externalDocumentId:
            return _error(
                409,
                "CONVERSATION_DOCUMENT_MISMATCH",
                "Conversation belongs to another document",
                request_id,
            )
        session_id = conversation["ragflow_session_id"]

    client = _query_client()
    try:
        chat_name = f"enterprise-demo-{doc.ragflow_dataset_id}"
        chats = await client.list_chats(name=chat_name)
        chat_id = next(
            (c.get("id") for c in chats if c.get("name") == chat_name),
            None,
        )
        if not chat_id:
            created = await client.create_chat(
                chat_name, [doc.ragflow_dataset_id]
            )
            chat_id = (created.get("data") or {}).get("id", "")
        if not chat_id:
            raise RAGFlowAPIError("Chat id missing after create", 502)

        completion = await client.chat_completion(
            chat_id,
            req.question,
            session_id=session_id,
            doc_ids=list(scope.document_ids),
        )
    except RAGFlowAPIError as e:
        code = (
            "RAGFLOW_API_INCOMPATIBLE"
            if e.status_code and 400 <= e.status_code < 500
            else "RAGFLOW_UNAVAILABLE"
        )
        logger.warning(
            "Query demo ask RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        return _error(
            503 if code == "RAGFLOW_UNAVAILABLE" else 502,
            code,
            "RAGFlow service error",
            request_id,
        )

    data = completion.get("data", {}) if isinstance(completion, dict) else {}
    reference = data.get("reference", {}) if isinstance(data, dict) else {}
    raw_chunks = (
        reference.get("chunks", [])
        if isinstance(reference, dict)
        else []
    )
    raw_citations = [
        _to_citation(c, index)
        for index, c in enumerate(raw_chunks)
        if isinstance(c, dict)
    ]
    out_of_scope = [
        c
        for c in raw_citations
        if c.documentId and c.documentId not in set(scope.document_ids)
    ]
    if out_of_scope:
        logger.warning(
            "Query demo RAGFlow returned out-of-scope chunks "
            "document_ids=%s request_id=%s",
            sorted({c.documentId for c in out_of_scope}),
            request_id,
        )
        return _error(
            502,
            "RAGFLOW_SCOPE_VIOLATION",
            "RAGFlow retrieval returned an out-of-scope document",
            request_id,
        )
    citations = _filter_citations(raw_citations, scope)
    ragflow_session_id = (
        data.get("session_id") if isinstance(data, dict) else None
    )

    await conversation_store.upsert_conversation_map(
        db,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        conversation_id=conversation_id,
        ragflow_chat_id=chat_id,
        ragflow_session_id=ragflow_session_id,
        external_document_id=req.externalDocumentId,
    )
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        message_id=str(uuid.uuid4()),
        role="user",
        status="completed",
    )
    await conversation_store.add_message(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
        message_id=str(uuid.uuid4()),
        role="assistant",
        status="completed",
    )

    return DemoAskResponse(
        answer=data.get("answer", "") if isinstance(data, dict) else "",
        citations=citations,
        conversationId=conversation_id,
        ragflowSessionId=ragflow_session_id,
    )


def _session_messages_to_payload(
    session_data: dict, scope: AclScope
) -> list[dict]:
    data = session_data.get("data", {}) if isinstance(session_data, dict) else {}
    raw_messages = data.get("messages", []) if isinstance(data, dict) else []
    references = data.get("reference", []) if isinstance(data, dict) else []
    ref_index = 0
    messages = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        citations: list[DemoCitation] = []
        if role == "assistant":
            reference = (
                references[ref_index]
                if ref_index < len(references)
                else {}
            )
            ref_index += 1
            chunks = (
                reference.get("chunks", [])
                if isinstance(reference, dict)
                else []
            )
            citations = _filter_citations(
                [
                    _to_citation(c, index)
                    for index, c in enumerate(chunks)
                    if isinstance(c, dict)
                ],
                scope,
            )
        messages.append(
            {
                "messageId": msg.get("id") or "",
                "role": role,
                "content": msg.get("content", ""),
                "citations": [c.model_dump() for c in citations],
                "status": "completed",
                "createdAt": msg.get("created_at") or "",
            }
        )
    return messages


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    db=Depends(get_db),
    principal: UserPrincipal = Depends(require_user_principal),
):
    request_id = str(uuid.uuid4())
    await conversation_store.ensure_schema(db)
    conversation = await conversation_store.get_conversation_map(
        db,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        business_user_id=principal.business_user_id,
    )
    if not conversation:
        return _error(
            404,
            "CONVERSATION_NOT_FOUND",
            "Conversation not found",
            request_id,
        )

    doc, scope, acl_error = await _resolve_authorized_document(
        db,
        principal,
        conversation["external_document_id"],
        request_id,
    )
    if acl_error:
        return acl_error
    if doc is None:
        return _error(
            404, "DOCUMENT_NOT_FOUND", "Document not found", request_id
        )

    ragflow_chat_id = conversation.get("ragflow_chat_id")
    ragflow_session_id = conversation.get("ragflow_session_id")
    if ragflow_chat_id and ragflow_session_id:
        try:
            session_data = await _query_client().get_session(
                ragflow_chat_id, ragflow_session_id
            )
            messages = _session_messages_to_payload(session_data, scope)
            if messages:
                return {
                    "conversationId": conversation[
                        "business_conversation_id"
                    ],
                    "ragflowSessionId": ragflow_session_id,
                    "messages": messages,
                }
        except RAGFlowAPIError:
            logger.warning(
                "Query demo session read failed conversation_id=%s",
                conversation_id,
            )

    messages = [
        {
            "messageId": row["message_id"],
            "role": row["role"],
            "content": "",
            "citations": [],
            "status": row["status"],
            "createdAt": row["created_at"],
        }
        for row in await conversation_store.list_messages(db, conversation_id)
    ]
    return {
        "conversationId": conversation["business_conversation_id"],
        "ragflowSessionId": conversation["ragflow_session_id"],
        "messages": messages,
    }
