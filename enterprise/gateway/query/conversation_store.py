"""Conversation and message mapping for the query demo router.

Keeps RAGFlow as the message truth source: this store persists the business
conversation mapping plus lightweight message records used by the demo UI.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import add_column_if_missing, exec_sql, fetchall, fetchone

import json
from datetime import datetime, timezone

async def ensure_schema(db) -> None:
    from enterprise.gateway.db import GatewayDatabase

    async def _migrate(conn: AsyncConnection) -> None:
        for column in ("source_version_id", "asset_id"):
            await add_column_if_missing(conn, "ext_conversation_map", column, "TEXT")
        for column in ("ragflow_message_id", "content", "citations_json"):
            await add_column_if_missing(conn, "ext_conversation_message", column, "TEXT")
        for column in ("image_id", "positions_json", "evidence_json"):
            await add_column_if_missing(conn, "ext_conversation_citation", column, "TEXT")

    if isinstance(db, GatewayDatabase):
        async with db.transaction(write=True) as conn:
            await _migrate(conn)
        return
    await _migrate(db)


async def get_conversation_map(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    row = await fetchone(conn, """SELECT id, tenant_id, business_conversation_id, business_user_id,
                  ragflow_chat_id, ragflow_session_id, external_document_id,
                  source_version_id, asset_id,
                  equipment_id, fixed_asset_no, current_fault_code,
                  status, created_at, last_message_at
           FROM ext_conversation_map
           WHERE business_conversation_id=?
             AND tenant_id=?
             AND business_user_id=?
           LIMIT 1""", (conversation_id, tenant_id, business_user_id))
    return dict(row) if row else None


async def upsert_conversation_map(
    conn,
    *,
    tenant_id: str,
    business_user_id: str,
    conversation_id: str,
    ragflow_chat_id: str,
    ragflow_session_id: str | None,
    external_document_id: str,
    source_version_id: str | None = None,
    asset_id: str | None = None,
    equipment_id: str | None = None,
    fixed_asset_no: str | None = None,
    current_fault_code: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    result = await exec_sql(conn,
        """INSERT INTO ext_conversation_map
           (tenant_id, business_conversation_id, business_user_id,
            ragflow_chat_id, ragflow_session_id, external_document_id,
            source_version_id, asset_id,
            equipment_id, fixed_asset_no, current_fault_code,
            status, created_at, last_message_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
           ON CONFLICT(tenant_id, business_user_id, business_conversation_id)
           DO UPDATE SET
             ragflow_chat_id=excluded.ragflow_chat_id,
             ragflow_session_id=COALESCE(
                 excluded.ragflow_session_id, ext_conversation_map.ragflow_session_id
             ),
             external_document_id=excluded.external_document_id,
             source_version_id=COALESCE(
                 excluded.source_version_id, ext_conversation_map.source_version_id
             ),
             asset_id=COALESCE(excluded.asset_id, ext_conversation_map.asset_id),
             status=excluded.status,
             last_message_at=excluded.last_message_at""",
        (
            tenant_id,
            conversation_id,
            business_user_id,
            ragflow_chat_id,
            ragflow_session_id,
            external_document_id,
            source_version_id,
            asset_id,
            equipment_id,
            fixed_asset_no,
            current_fault_code,
            now,
            now,
        ),
    )


async def create_conversation(
    conn,
    *,
    tenant_id: str,
    business_user_id: str,
    conversation_id: str,
    equipment_id: str | None = None,
    fixed_asset_no: str | None = None,
    current_fault_code: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    result = await exec_sql(conn,
        """INSERT INTO ext_conversation
           (conversation_id, tenant_id, business_user_id,
            equipment_id, fixed_asset_no, current_fault_code,
            status, created_at, last_message_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
           ON CONFLICT(tenant_id, business_user_id, conversation_id)
           DO NOTHING""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            equipment_id,
            fixed_asset_no,
            current_fault_code,
            now,
            now,
        ),
    )
    return await get_conversation(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def get_conversation(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    row = await fetchone(
        conn,
        """SELECT id, conversation_id, tenant_id, business_user_id,
                  equipment_id, fixed_asset_no, current_fault_code,
                  status, ragflow_chat_id, ragflow_session_id,
                  created_at, last_message_at
           FROM ext_conversation
           WHERE conversation_id=?
             AND tenant_id=?
             AND business_user_id=?
           LIMIT 1""",
        (conversation_id, tenant_id, business_user_id),
    )
    return dict(row) if row else None


async def update_conversation_identity(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
) -> None:
    await exec_sql(
        conn,
        """UPDATE ext_conversation
           SET equipment_id=?, fixed_asset_no=?
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (
            equipment_id,
            fixed_asset_no,
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )


async def update_conversation_mapping(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    ragflow_chat_id: str,
    ragflow_session_id: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    result = await exec_sql(conn,
        """UPDATE ext_conversation
           SET ragflow_chat_id=?,
               ragflow_session_id=COALESCE(?, ragflow_session_id),
               last_message_at=?
           WHERE conversation_id=?
             AND tenant_id=?
             AND business_user_id=?""",
        (
            ragflow_chat_id,
            ragflow_session_id,
            now,
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )


async def add_message(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    message_id: str,
    role: str,
    status: str = "completed",
    ragflow_message_id: str | None = None,
    content: str | None = None,
    citations: list[dict] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    citations = citations or []
    citations_json = json.dumps(citations, ensure_ascii=False)
    result = await exec_sql(conn,
        """INSERT INTO ext_conversation_message
           (conversation_id, tenant_id, business_user_id, message_id,
            role, status, ragflow_message_id, content, citations_json,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            message_id,
            role,
            status,
            ragflow_message_id,
            content,
            citations_json,
            now,
            now,
        ),
    )
    for citation in citations:
        result = await exec_sql(conn,
            """INSERT INTO ext_conversation_citation
               (citation_id, message_id, conversation_id, tenant_id,
                business_user_id, source_type, title, document_id,
                ragflow_document_id, chunk_id, source_version_id, asset_id,
                 page_no, bbox_json, image_id, positions_json, evidence_json,
                 excerpt, record_type, record_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                citation.get("citationId"),
                message_id,
                conversation_id,
                tenant_id,
                business_user_id,
                citation.get("sourceType", "document"),
                citation.get("title", ""),
                citation.get("documentId"),
                citation.get("ragflowDocumentId"),
                citation.get("chunkId"),
                citation.get("versionId"),
                citation.get("assetId"),
                citation.get("pageNo"),
                json.dumps(citation["bbox"], ensure_ascii=False)
                if citation.get("bbox") is not None
                else None,
                citation.get("imageId"),
                json.dumps(citation.get("positions") or [], ensure_ascii=False),
                json.dumps(citation.get("evidence"), ensure_ascii=False)
                if citation.get("evidence") is not None
                else None,
                citation.get("excerpt"),
                citation.get("recordType"),
                citation.get("recordId"),
                now,
            ),
        )


async def update_message_status(
    conn,
    *,
    message_id: str,
    status: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    result = await exec_sql(conn,
        """UPDATE ext_conversation_message
           SET status=?, updated_at=?
           WHERE message_id=?""",
        (status, now, message_id),
    )


async def list_messages(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT message_id, role, status, content, created_at
           FROM ext_conversation_message
           WHERE conversation_id=?
             AND tenant_id=?
             AND business_user_id=?
           ORDER BY id ASC""",
        (conversation_id, tenant_id, business_user_id),
    )
    messages = []
    for row in rows:
        citations = await list_citations_for_message(
            conn,
            message_id=row["message_id"],
            conversation_id=conversation_id,
        )
        messages.append(
            {
                "messageId": row["message_id"],
                "role": row["role"],
                "content": row["content"] or "",
                "status": row["status"],
                "citations": citations,
                "createdAt": row["created_at"],
            }
        )
    return messages


async def list_citations_for_message(
    conn,
    *,
    message_id: str,
    conversation_id: str,
) -> list[dict]:
    rows = await fetchall(conn, """SELECT citation_id, message_id, conversation_id,
                  source_type, title, document_id, ragflow_document_id,
                  chunk_id, source_version_id, asset_id, page_no,
                  bbox_json, image_id, positions_json, evidence_json,
                  excerpt, record_type, record_id, created_at
           FROM ext_conversation_citation
           WHERE message_id=? AND conversation_id=?
           ORDER BY id ASC""", (message_id, conversation_id))
    result = []
    for row in rows:
        bbox = None
        if row["bbox_json"]:
            try:
                bbox = json.loads(row["bbox_json"])
            except json.JSONDecodeError:
                bbox = None
        positions = []
        if row["positions_json"]:
            try:
                positions = json.loads(row["positions_json"])
            except json.JSONDecodeError:
                positions = []
        evidence = None
        if row["evidence_json"]:
            try:
                evidence = json.loads(row["evidence_json"])
            except json.JSONDecodeError:
                evidence = None
        result.append(
            {
                "citationId": row["citation_id"],
                "sourceType": row["source_type"],
                "title": row["title"],
                "documentId": row["document_id"],
                "ragflowDocumentId": row["ragflow_document_id"],
                "chunkId": row["chunk_id"],
                "versionId": row["source_version_id"],
                "assetId": row["asset_id"],
                "pageNo": row["page_no"],
                "bbox": bbox,
                "imageId": row["image_id"],
                "positions": positions,
                "evidence": evidence,
                "excerpt": row["excerpt"],
                "recordType": row["record_type"],
                "recordId": row["record_id"],
                "createdAt": row["created_at"],
            }
        )
    return result


async def get_citation(
    conn,
    *,
    citation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    row = await fetchone(
        conn,
        """SELECT citation_id, message_id, conversation_id, tenant_id,
                  business_user_id, source_type, title, document_id,
                  ragflow_document_id, chunk_id, source_version_id, asset_id,
                   page_no, bbox_json, image_id, positions_json, evidence_json,
                   excerpt, record_type, record_id, created_at
           FROM ext_conversation_citation
           WHERE citation_id=?
             AND tenant_id=?
             AND business_user_id=?
           LIMIT 1""",
        (citation_id, tenant_id, business_user_id),
    )
    if not row:
        return None
    bbox = None
    if row["bbox_json"]:
        try:
            bbox = json.loads(row["bbox_json"])
        except json.JSONDecodeError:
            bbox = None
    positions = []
    if row["positions_json"]:
        try:
            positions = json.loads(row["positions_json"])
        except json.JSONDecodeError:
            positions = []
    evidence = None
    if row["evidence_json"]:
        try:
            evidence = json.loads(row["evidence_json"])
        except json.JSONDecodeError:
            evidence = None
    return {
        "citationId": row["citation_id"],
        "messageId": row["message_id"],
        "conversationId": row["conversation_id"],
        "sourceType": row["source_type"],
        "title": row["title"],
        "documentId": row["document_id"],
        "ragflowDocumentId": row["ragflow_document_id"],
        "chunkId": row["chunk_id"],
        "versionId": row["source_version_id"],
        "assetId": row["asset_id"],
        "pageNo": row["page_no"],
        "bbox": bbox,
        "imageId": row["image_id"],
        "positions": positions,
        "evidence": evidence,
        "excerpt": row["excerpt"],
        "recordType": row["record_type"],
        "recordId": row["record_id"],
        "createdAt": row["created_at"],
    }


async def list_message_statuses(
    conn,
    *,
    conversation_id: str,
) -> dict[tuple[str, str], str]:
    """Map RAGFlow message ids to the gateway's persisted business status."""
    rows = await fetchall(
        conn,
        """SELECT ragflow_message_id, role, status
           FROM ext_conversation_message
           WHERE conversation_id=? AND ragflow_message_id IS NOT NULL""",
        (conversation_id,),
    )
    return {
        (row["ragflow_message_id"], row["role"]): row["status"]
        for row in rows
    }
