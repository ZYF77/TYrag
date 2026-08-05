"""Temporary enterprise knowledge-base demo closed loop.

Routes are deliberately kept outside the frozen WP-02 contract:
  - POST /enterprise/api/v1/demo/documents
  - POST /enterprise/api/v1/demo/ask

Only RAGFlow public REST APIs are used. No RAGFlow source is modified.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    get_mapping,
    insert_mapping,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
    RAGFlowDocumentStub,
)
from enterprise.gateway.sync.status_mapping import enterprise_stage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/api/v1/demo", tags=["demo"])

CREATE_DEMO_CONVERSATION = """
CREATE TABLE IF NOT EXISTS demo_conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL DEFAULT 'demo',
    external_document_id TEXT NOT NULL,
    ragflow_chat_id TEXT,
    ragflow_session_id TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_demo_conversation_doc
    ON demo_conversation(tenant_id, external_document_id);
"""


async def get_db():
    """Reuse the app-level SQLite connection without importing app eagerly."""
    from enterprise.gateway import app as app_module

    dep = app_module.app.dependency_overrides.get(
        app_module.get_db, app_module.get_db
    )
    return await dep()


async def _ensure_demo_schema(db) -> None:
    await db.executescript(CREATE_DEMO_CONVERSATION)
    await db.commit()


async def _get_conversation_rows(db, conversation_id: str) -> list[dict]:
    async with db.execute(
        """SELECT id, conversation_id, tenant_id, business_user_id,
                  external_document_id, ragflow_chat_id, ragflow_session_id,
                  question, answer, citations_json, created_at
           FROM demo_conversation
           WHERE conversation_id=?
           ORDER BY id ASC""",
        (conversation_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _save_conversation_turn(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    external_document_id: str,
    ragflow_chat_id: str,
    ragflow_session_id: str | None,
    question: str,
    answer: str,
    citations: list[DemoCitation] | None = None,
) -> None:
    import json
    from datetime import datetime, timezone

    await db.execute(
        """INSERT INTO demo_conversation
           (conversation_id, tenant_id, business_user_id, external_document_id,
            ragflow_chat_id, ragflow_session_id, question, answer,
            citations_json, created_at)
           VALUES (?, ?, 'demo', ?, ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            external_document_id,
            ragflow_chat_id,
            ragflow_session_id,
            question,
            answer,
            json.dumps(
                [c.model_dump() for c in (citations or [])],
                ensure_ascii=False,
            ),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()


def _demo_error(
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


def _demo_client():
    if os.environ.get("ENTERPRISE_TEST_MODE") == "1":
        return RAGFlowDemoStub()
    return RAGFlowDemoClient(
        api_key=os.environ.get("RAGFLOW_API_KEY", "stub-key")
    )


class RAGFlowDemoClient(RAGFlowDocumentClient):
    """Demo-only additions on top of the existing document client."""

    @staticmethod
    def _require_ok(result: dict) -> dict:
        if isinstance(result, dict) and result.get("code") not in (0, None):
            raise RAGFlowAPIError(
                str(result.get("message") or "RAGFlow returned an error"), 200
            )
        return result

    async def start_parsing(
        self, dataset_id: str, document_ids: list[str],
        request_id: str | None = None
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "POST",
            f"/api/v1/datasets/{dataset_id}/chunks",
            rid,
            json_data={"document_ids": document_ids},
        )
        return self._require_ok(result)

    async def list_chats(
        self, name: str | None = None, request_id: str | None = None
    ) -> list[dict]:
        rid = request_id or self._new_request_id()
        path = "/api/v1/chats"
        if name:
            from urllib.parse import quote

            path += f"?name={quote(name)}"
        result = await self._run_sync(
            self._sync_request, "GET", path, rid
        )
        result = self._require_ok(result)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        return data.get("chats", []) if isinstance(data, dict) else []

    async def create_chat(
        self, name: str, dataset_ids: list[str],
        request_id: str | None = None
    ) -> dict:
        rid = request_id or self._new_request_id()
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chats",
            rid,
            json_data={"name": name, "dataset_ids": dataset_ids},
        )
        return self._require_ok(result)

    async def chat_completion(
        self, chat_id: str, question: str,
        session_id: str | None = None, request_id: str | None = None
    ) -> dict:
        rid = request_id or self._new_request_id()
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "stream": False,
        }
        if session_id:
            body["session_id"] = session_id
        result = await self._run_sync(
            self._sync_request,
            "POST",
            "/api/v1/chat/completions",
            rid,
            json_data=body,
        )
        return self._require_ok(result)


class RAGFlowDemoStub(RAGFlowDocumentStub):
    """Offline stub for the demo closed loop."""

    def __init__(self) -> None:
        super().__init__()
        self._chats: dict[str, dict] = {}

    async def start_parsing(
        self, dataset_id: str, document_ids: list[str],
        request_id: str | None = None
    ) -> dict:
        return {"code": 0, "data": True}

    async def list_chats(
        self, name: str | None = None, request_id: str | None = None
    ) -> list[dict]:
        if name is None:
            return list(self._chats.values())
        return [c for c in self._chats.values() if c.get("name") == name]

    async def create_chat(
        self, name: str, dataset_ids: list[str],
        request_id: str | None = None
    ) -> dict:
        chat = {
            "id": f"chat-{uuid.uuid4().hex[:12]}",
            "name": name,
            "dataset_ids": dataset_ids,
        }
        self._chats[chat["id"]] = chat
        return {"code": 0, "data": chat}

    async def chat_completion(
        self, chat_id: str, question: str,
        session_id: str | None = None, request_id: str | None = None
    ) -> dict:
        return {
            "code": 0,
            "data": {
                "answer": f"stub answer for: {question}",
                "session_id": session_id or "stub-session",
                "reference": {
                    "chunks": [
                        {
                            "id": "chunk-1",
                            "content": "故障码 E-104 时先检查液压油位。",
                            "document_id": "doc-1",
                            "document_name": "manual.pdf",
                            "positions": [[3, 0.1, 0.2, 0.8, 0.4]],
                        }
                    ]
                },
            },
        }


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
    tenantId: str = Field(default="demo", min_length=1, max_length=64)
    sourceSystem: str = Field(default="DEMO", min_length=1, max_length=64)
    sourceVersionId: str = Field(default="v1", min_length=1, max_length=64)
    conversationId: str | None = None
    sessionId: str | None = None


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


@router.post("/documents")
async def upload_demo_document(
    request: Request,
    db=Depends(get_db),
    principal=Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    tenant_id = request.query_params.get("tenantId", "demo").strip()
    source_system = request.query_params.get("sourceSystem", "DEMO").strip()
    external_document_id = request.query_params.get(
        "externalDocumentId", ""
    ).strip()
    source_version_id = request.query_params.get(
        "sourceVersionId", "v1"
    ).strip()
    file_name = request.headers.get("X-File-Name", "").strip()

    if not external_document_id or len(external_document_id) > 128:
        return _demo_error(
            422,
            "VALIDATION_ERROR",
            "externalDocumentId is required and must be <= 128 chars",
            request_id,
        )
    if not source_system or len(source_system) > 64:
        return _demo_error(
            422,
            "VALIDATION_ERROR",
            "sourceSystem is required and must be <= 64 chars",
            request_id,
        )

    content = await request.body()
    if not content:
        return _demo_error(
            422, "VALIDATION_ERROR", "PDF body is empty", request_id
        )
    content_type = request.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("application/pdf"):
        return _demo_error(
            422,
            "VALIDATION_ERROR",
            "Content-Type must be application/pdf",
            request_id,
        )
    if not file_name:
        file_name = f"{external_document_id}.pdf"
    if len(file_name) > 255:
        return _demo_error(
            422,
            "VALIDATION_ERROR",
            "X-File-Name must be <= 255 chars",
            request_id,
        )

    sha256 = hashlib.sha256(content).hexdigest()
    existing = await get_mapping(
        db, tenant_id, source_system, external_document_id, source_version_id
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
        source_system=source_system,
        external_document_id=external_document_id,
        source_version_id=source_version_id,
        event_id=str(uuid.uuid4()),
        sha256=sha256,
        file_name=file_name,
        media_type="application/pdf",
        sync_status="received",
    )
    try:
        doc = await insert_mapping(db, doc)
    except Exception:
        logger.exception("Demo document insert failed")
        return _demo_error(
            500, "INTERNAL_ERROR", "Internal service error", request_id
        )

    await update_mapping_status(db, doc, "validated")
    client = _demo_client()
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
            return _demo_error(
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
    except RAGFlowAPIError as e:
        code = (
            "RAGFLOW_API_INCOMPATIBLE"
            if e.status_code and 400 <= e.status_code < 500
            else "RAGFLOW_UNAVAILABLE"
        )
        await update_mapping_status(
            db, doc, "failed", error_code=code, error_message="RAGFlow API error"
        )
        logger.warning(
            "Demo RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        return _demo_error(
            503 if code == "RAGFLOW_UNAVAILABLE" else 502,
            code,
            "RAGFlow service error",
            request_id,
        )

    return DemoDocumentResponse(
        externalDocumentId=external_document_id,
        sourceVersionId=source_version_id,
        ragflowDatasetId=dataset_id,
        ragflowDocumentId=doc.ragflow_document_id,
        status="parsing",
        stage="parsing",
    )


@router.post("/ask", response_model=DemoAskResponse)
async def ask(
    req: DemoAskRequest,
    db=Depends(get_db),
    principal=Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    doc = await get_mapping(
        db,
        req.tenantId,
        req.sourceSystem,
        req.externalDocumentId,
        req.sourceVersionId,
    )
    if not doc:
        return _demo_error(
            404, "DOCUMENT_NOT_FOUND", "Document not found", request_id
        )
    if doc.sync_status != "ready":
        return _demo_error(
            409,
            "DOCUMENT_NOT_READY",
            f"Document status is {doc.sync_status}, expected ready",
            request_id,
        )

    await _ensure_demo_schema(db)
    conversation_id = req.conversationId or str(uuid.uuid4())
    session_id = req.sessionId
    if req.conversationId and not session_id:
        rows = await _get_conversation_rows(db, conversation_id)
        if not rows:
            return _demo_error(
                404,
                "CONVERSATION_NOT_FOUND",
                "Conversation not found",
                request_id,
            )
        last_row = rows[-1]
        if (
            last_row["tenant_id"] != req.tenantId
            or last_row["external_document_id"] != req.externalDocumentId
        ):
            return _demo_error(
                409,
                "CONVERSATION_DOCUMENT_MISMATCH",
                "Conversation belongs to another document",
                request_id,
            )
        session_id = last_row["ragflow_session_id"] or None

    client = _demo_client()
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
            chat_id, req.question, session_id=session_id
        )
    except RAGFlowAPIError as e:
        code = (
            "RAGFLOW_API_INCOMPATIBLE"
            if e.status_code and 400 <= e.status_code < 500
            else "RAGFLOW_UNAVAILABLE"
        )
        logger.warning(
            "Demo ask RAGFlow error code=%s status=%s request_id=%s",
            code,
            e.status_code,
            request_id,
        )
        return _demo_error(
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
    citations = [
        _to_citation(c, index)
        for index, c in enumerate(raw_chunks)
        if isinstance(c, dict)
    ]
    ragflow_session_id = (
        data.get("session_id") if isinstance(data, dict) else None
    )
    await _save_conversation_turn(
        db,
        conversation_id=conversation_id,
        tenant_id=req.tenantId,
        external_document_id=req.externalDocumentId,
        ragflow_chat_id=chat_id,
        ragflow_session_id=ragflow_session_id,
        question=req.question,
        answer=data.get("answer", "") if isinstance(data, dict) else "",
        citations=citations,
    )
    return DemoAskResponse(
        answer=data.get("answer", "") if isinstance(data, dict) else "",
        citations=citations,
        conversationId=conversation_id,
        ragflowSessionId=ragflow_session_id,
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    db=Depends(get_db),
    principal=Depends(require_service_principal),
):
    request_id = str(uuid.uuid4())
    await _ensure_demo_schema(db)
    rows = await _get_conversation_rows(db, conversation_id)
    if not rows:
        return _demo_error(
            404,
            "CONVERSATION_NOT_FOUND",
            "Conversation not found",
            request_id,
        )

    import json

    messages = []
    for row in rows:
        try:
            citations = json.loads(row["citations_json"])
        except (TypeError, ValueError):
            citations = []
        messages.append(
            {
                "question": row["question"],
                "answer": row["answer"],
                "citations": citations,
                "ragflowSessionId": row["ragflow_session_id"],
                "createdAt": row["created_at"],
            }
        )
    return {"conversationId": conversation_id, "messages": messages}
