"""Conversation and message mapping for the query demo router.

Keeps RAGFlow as the message truth source: this store persists the business
conversation mapping plus lightweight message records used by the demo UI.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

CREATE_EXT_CONVERSATION_MAP = """
CREATE TABLE IF NOT EXISTS ext_conversation_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    business_conversation_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    ragflow_chat_id TEXT,
    ragflow_session_id TEXT,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT,
    asset_id TEXT,
    equipment_id TEXT,
    fixed_asset_no TEXT,
    current_fault_code TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_message_at TEXT,
    UNIQUE(tenant_id, business_user_id, business_conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_user
    ON ext_conversation_map(tenant_id, business_user_id);
"""

CREATE_EXT_CONVERSATION_MESSAGE = """
CREATE TABLE IF NOT EXISTS ext_conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    ragflow_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_message_conv
    ON ext_conversation_message(conversation_id);
"""

CREATE_EXT_CONVERSATION = """
CREATE TABLE IF NOT EXISTS ext_conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    equipment_id TEXT,
    fixed_asset_no TEXT,
    current_fault_code TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    ragflow_chat_id TEXT,
    ragflow_session_id TEXT,
    created_at TEXT NOT NULL,
    last_message_at TEXT,
    UNIQUE(tenant_id, business_user_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_owner
    ON ext_conversation(tenant_id, business_user_id);
"""

CREATE_EXT_CONVERSATION_CITATION = """
CREATE TABLE IF NOT EXISTS ext_conversation_citation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    citation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'document',
    title TEXT NOT NULL,
    document_id TEXT,
    ragflow_document_id TEXT,
    chunk_id TEXT,
    source_version_id TEXT,
    asset_id TEXT,
    page_no INTEGER,
    bbox_json TEXT,
    image_id TEXT,
    positions_json TEXT,
    evidence_json TEXT,
    excerpt TEXT,
    record_type TEXT,
    record_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(citation_id)
);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_citation_message
    ON ext_conversation_citation(message_id);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_citation_conv
    ON ext_conversation_citation(conversation_id);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_citation_doc
    ON ext_conversation_citation(document_id);
"""


async def ensure_schema(db) -> None:
    await db.executescript(CREATE_EXT_CONVERSATION_MAP)
    await db.executescript(CREATE_EXT_CONVERSATION_MESSAGE)
    await db.executescript(CREATE_EXT_CONVERSATION)
    await db.executescript(CREATE_EXT_CONVERSATION_CITATION)
    async with db.execute(
        "PRAGMA table_info(ext_conversation_map)"
    ) as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "source_version_id" not in columns:
        await db.execute(
            "ALTER TABLE ext_conversation_map ADD COLUMN source_version_id TEXT"
        )
    if "asset_id" not in columns:
        await db.execute(
            "ALTER TABLE ext_conversation_map ADD COLUMN asset_id TEXT"
        )
    async with db.execute(
        "PRAGMA table_info(ext_conversation_message)"
    ) as cursor:
        message_columns = {row["name"] for row in await cursor.fetchall()}
    if "ragflow_message_id" not in message_columns:
        await db.execute(
            "ALTER TABLE ext_conversation_message "
            "ADD COLUMN ragflow_message_id TEXT"
        )
    if "content" not in message_columns:
        await db.execute(
            "ALTER TABLE ext_conversation_message ADD COLUMN content TEXT"
        )
    if "citations_json" not in message_columns:
        await db.execute(
            "ALTER TABLE ext_conversation_message "
            "ADD COLUMN citations_json TEXT"
        )
    async with db.execute(
        "PRAGMA table_info(ext_conversation_citation)"
    ) as cursor:
        citation_columns = {row["name"] for row in await cursor.fetchall()}
    for column in ("image_id", "positions_json", "evidence_json"):
        if column not in citation_columns:
            await db.execute(
                f"ALTER TABLE ext_conversation_citation ADD COLUMN {column} TEXT"
            )
    await db.commit()


async def get_conversation_map(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    async with db.execute(
        """SELECT id, tenant_id, business_conversation_id, business_user_id,
                  ragflow_chat_id, ragflow_session_id, external_document_id,
                  source_version_id, asset_id,
                  equipment_id, fixed_asset_no, current_fault_code,
                  status, created_at, last_message_at
           FROM ext_conversation_map
           WHERE business_conversation_id=?
             AND tenant_id=?
             AND business_user_id=?
           LIMIT 1""",
        (conversation_id, tenant_id, business_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_conversation_map(
    db,
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
    await db.execute(
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
                 excluded.ragflow_session_id, ragflow_session_id
             ),
             external_document_id=excluded.external_document_id,
             source_version_id=COALESCE(
                 excluded.source_version_id, source_version_id
             ),
             asset_id=COALESCE(excluded.asset_id, asset_id),
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
    await db.commit()


async def create_conversation(
    db,
    *,
    tenant_id: str,
    business_user_id: str,
    conversation_id: str,
    equipment_id: str | None = None,
    fixed_asset_no: str | None = None,
    current_fault_code: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
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
    await db.commit()
    return await get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def get_conversation(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    async with db.execute(
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
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def update_conversation_mapping(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    ragflow_chat_id: str,
    ragflow_session_id: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
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
    await db.commit()


async def add_message(
    db,
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
    await db.execute(
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
        await db.execute(
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
    await db.commit()


async def update_message_status(
    db,
    *,
    message_id: str,
    status: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE ext_conversation_message
           SET status=?, updated_at=?
           WHERE message_id=?""",
        (status, now, message_id),
    )
    await db.commit()


async def list_messages(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> list[dict]:
    async with db.execute(
        """SELECT message_id, role, status, content, created_at
           FROM ext_conversation_message
           WHERE conversation_id=?
             AND tenant_id=?
             AND business_user_id=?
           ORDER BY id ASC""",
        (conversation_id, tenant_id, business_user_id),
    ) as cursor:
        rows = await cursor.fetchall()
    messages = []
    for row in rows:
        citations = await list_citations_for_message(
            db,
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
    db,
    *,
    message_id: str,
    conversation_id: str,
) -> list[dict]:
    async with db.execute(
        """SELECT citation_id, message_id, conversation_id,
                  source_type, title, document_id, ragflow_document_id,
                  chunk_id, source_version_id, asset_id, page_no,
                  bbox_json, image_id, positions_json, evidence_json,
                  excerpt, record_type, record_id, created_at
           FROM ext_conversation_citation
           WHERE message_id=? AND conversation_id=?
           ORDER BY id ASC""",
        (message_id, conversation_id),
    ) as cursor:
        rows = await cursor.fetchall()
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
    db,
    *,
    citation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> dict | None:
    async with db.execute(
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
    ) as cursor:
        row = await cursor.fetchone()
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
    db,
    *,
    conversation_id: str,
) -> dict[tuple[str, str], str]:
    """Map RAGFlow message ids to the gateway's persisted business status."""
    async with db.execute(
        """SELECT ragflow_message_id, role, status
           FROM ext_conversation_message
           WHERE conversation_id=? AND ragflow_message_id IS NOT NULL""",
        (conversation_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {
        (row["ragflow_message_id"], row["role"]): row["status"]
        for row in rows
    }
