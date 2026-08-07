"""Conversation and message mapping for the query demo router.

Keeps RAGFlow as the message truth source: this store persists the business
conversation mapping plus lightweight message records used by the demo UI.
"""
from __future__ import annotations

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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ext_conversation_message_conv
    ON ext_conversation_message(conversation_id);
"""


async def ensure_schema(db) -> None:
    await db.executescript(CREATE_EXT_CONVERSATION_MAP)
    await db.executescript(CREATE_EXT_CONVERSATION_MESSAGE)
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


async def add_message(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    message_id: str,
    role: str,
    status: str = "completed",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO ext_conversation_message
           (conversation_id, tenant_id, business_user_id, message_id,
            role, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            message_id,
            role,
            status,
            now,
            now,
        ),
    )
    await db.commit()
