"""Enterprise-owned conversation truth store for the v2 external API."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_v2_conversation (
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New conversation',
    equipment_id TEXT,
    fixed_asset_no TEXT,
    fault_code TEXT,
    context_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    ragflow_chat_id TEXT,
    ragflow_session_id TEXT,
    registry_version TEXT,
    context_resolved_at TEXT,
    first_message_at TEXT,
    created_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, business_user_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_v2_conversation_page
    ON ext_v2_conversation(
        tenant_id, business_user_id, last_message_at DESC, conversation_id DESC
    );

CREATE TABLE IF NOT EXISTS ext_v2_message (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_v2_message_page
    ON ext_v2_message(
        conversation_id, tenant_id, business_user_id, created_at, message_id
    );

CREATE TABLE IF NOT EXISTS ext_v2_message_run (
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    lease_expires_at TEXT,
    user_message_id TEXT,
    assistant_message_id TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        conversation_id, tenant_id, business_user_id, client_message_id
    )
);

CREATE TABLE IF NOT EXISTS ext_v2_citation (
    citation_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    business_user_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_cursor(timestamp: str, item_id: str) -> str:
    raw = json.dumps([timestamp, item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid cursor") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("Invalid cursor")
    return value[0], value[1]


async def ensure_schema(db) -> None:
    await db.executescript(SCHEMA)
    migrations = {
        "ext_v2_conversation": {
            "registry_version": "TEXT",
            "context_resolved_at": "TEXT",
            "first_message_at": "TEXT",
        },
        "ext_v2_message_run": {
            "run_id": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'running'",
            "lease_expires_at": "TEXT",
            "user_message_id": "TEXT",
            "assistant_message_id": "TEXT",
        },
    }
    for table, columns in migrations.items():
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for column, definition in columns.items():
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
    # Existing candidate rows predate durable run identifiers.  They are
    # offline-only rows and can safely receive deterministic placeholders.
    await db.execute(
        """UPDATE ext_v2_message_run SET run_id=lower(hex(randomblob(16)))
           WHERE run_id IS NULL OR run_id=''"""
    )
    await db.commit()


def conversation_payload(row) -> dict:
    return {
        "conversationId": row["conversation_id"],
        "title": row["title"],
        "status": row["status"],
        "equipmentId": row["equipment_id"],
        "fixedAssetNo": row["fixed_asset_no"],
        "faultCode": row["fault_code"],
        "contextVersion": row["context_version"],
        "lastMessageAt": row["last_message_at"],
        "createdAt": row["created_at"],
    }


async def create_conversation(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    fault_code: str | None,
    registry_version: str | None = None,
    context_resolved_at: str | None = None,
) -> dict:
    now = utc_now()
    context_version = int(
        any(value is not None for value in (equipment_id, fixed_asset_no, fault_code))
    )
    await db.execute(
        """INSERT INTO ext_v2_conversation
           (conversation_id, tenant_id, business_user_id, title,
            equipment_id, fixed_asset_no, fault_code, context_version,
            status, ragflow_chat_id, ragflow_session_id, registry_version,
            context_resolved_at, first_message_at, created_at, last_message_at)
           VALUES (?, ?, ?, 'New conversation', ?, ?, ?, ?, 'active',
                   NULL, NULL, ?, ?, NULL, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            equipment_id,
            fixed_asset_no,
            fault_code,
            context_version,
            registry_version,
            context_resolved_at,
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
    db, *, conversation_id: str, tenant_id: str, business_user_id: str
) -> dict | None:
    async with db.execute(
        """SELECT * FROM ext_v2_conversation
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
           LIMIT 1""",
        (conversation_id, tenant_id, business_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def list_conversations(
    db,
    *,
    tenant_id: str,
    business_user_id: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None, bool]:
    marker = decode_cursor(cursor)
    where = "tenant_id=? AND business_user_id=?"
    params: list[object] = [tenant_id, business_user_id]
    if marker:
        where += (
            " AND (last_message_at < ? OR "
            "(last_message_at = ? AND conversation_id < ?))"
        )
        params.extend([marker[0], marker[0], marker[1]])
    params.append(limit + 1)
    async with db.execute(
        f"""SELECT * FROM ext_v2_conversation WHERE {where}
            ORDER BY last_message_at DESC, conversation_id DESC LIMIT ?""",
        tuple(params),
    ) as db_cursor:
        rows = await db_cursor.fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(page[-1]["last_message_at"], page[-1]["conversation_id"])
    return [conversation_payload(row) for row in page], next_cursor, has_more


async def update_context(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    fault_code: str | None,
    context_version: int,
    registry_version: str | None = None,
    context_resolved_at: str | None = None,
) -> dict:
    await db.execute(
        """UPDATE ext_v2_conversation
           SET equipment_id=?, fixed_asset_no=?, fault_code=?, context_version=?,
               registry_version=?, context_resolved_at=?
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (
            equipment_id,
            fixed_asset_no,
            fault_code,
            context_version,
            registry_version,
            context_resolved_at,
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )
    await db.commit()
    return await get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def archive_conversation(
    db, *, conversation_id: str, tenant_id: str, business_user_id: str
) -> dict:
    await db.execute(
        """UPDATE ext_v2_conversation SET status='archived'
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (conversation_id, tenant_id, business_user_id),
    )
    await db.commit()
    return await get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def get_message_run(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
) -> dict | None:
    async with db.execute(
        """SELECT * FROM ext_v2_message_run
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? LIMIT 1""",
        (conversation_id, tenant_id, business_user_id, client_message_id),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def reserve_message_run(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
    request_hash: str,
    run_id: str | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
    question: str | None = None,
    lease_seconds: int = 1800,
) -> dict | None:
    run_id = run_id or __import__("uuid").uuid4().hex
    lease_expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + lease_seconds,
        tz=timezone.utc,
    ).isoformat()
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        """INSERT INTO ext_v2_message_run
           (conversation_id, tenant_id, business_user_id, client_message_id,
             request_hash, run_id, status, lease_expires_at, user_message_id,
             assistant_message_id, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?)
           ON CONFLICT DO NOTHING""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            client_message_id,
            request_hash,
            run_id,
            lease_expires_at,
            user_message_id,
            assistant_message_id,
            utc_now(),
        ),
    )
    if cursor.rowcount == 1 and question is not None and user_message_id:
        now = utc_now()
        await db.execute(
            """INSERT INTO ext_v2_message
               (message_id, conversation_id, tenant_id, business_user_id, role,
                content, status, citations_json, created_at)
               VALUES (?, ?, ?, ?, 'user', ?, 'completed', '[]', ?)""",
            (
                user_message_id,
                conversation_id,
                tenant_id,
                business_user_id,
                question,
                now,
            ),
        )
        title = " ".join(question.split())[:80] or "New conversation"
        await db.execute(
            """UPDATE ext_v2_conversation
               SET last_message_at=?, first_message_at=COALESCE(first_message_at, ?),
                   title=CASE WHEN title='New conversation' THEN ? ELSE title END
               WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
            (
                now,
                now,
                title,
                conversation_id,
                tenant_id,
                business_user_id,
            ),
        )
    await db.commit()
    if cursor.rowcount != 1:
        return None
    return await get_message_run(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        client_message_id=client_message_id,
    )


async def complete_message_run(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
    result: dict,
    status: str = "completed",
    assistant_message_id: str | None = None,
) -> None:
    await db.execute(
        """UPDATE ext_v2_message_run
           SET result_json=?, status=?, assistant_message_id=?, lease_expires_at=NULL
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? AND status='running'""",
        (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            status,
            assistant_message_id,
            conversation_id,
            tenant_id,
            business_user_id,
            client_message_id,
        ),
    )
    await db.commit()


async def mark_expired_run_interrupted(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
) -> dict | None:
    """Turn an abandoned run into a stable, replayable failure."""
    now = utc_now()
    result = {
        "_error": {
            "statusCode": 503,
            "body": {
                "code": "RUN_INTERRUPTED",
                "message": "Message run lease expired before completion",
                "requestId": str(__import__("uuid").uuid4()),
                "retryable": False,
            },
        }
    }
    await db.execute(
        """UPDATE ext_v2_message_run
           SET status='failed', result_json=?, lease_expires_at=NULL
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? AND status='running'
             AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
        (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            conversation_id,
            tenant_id,
            business_user_id,
            client_message_id,
            now,
        ),
    )
    async with db.execute(
        """SELECT assistant_message_id FROM ext_v2_message_run
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=?""",
        (conversation_id, tenant_id, business_user_id, client_message_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row and row["assistant_message_id"]:
        await db.execute(
            """INSERT INTO ext_v2_message
               (message_id, conversation_id, tenant_id, business_user_id, role,
                content, status, citations_json, created_at)
               VALUES (?, ?, ?, ?, 'assistant', '', 'failed', '[]', ?)
               ON CONFLICT(message_id) DO NOTHING""",
            (
                row["assistant_message_id"],
                conversation_id,
                tenant_id,
                business_user_id,
                now,
            ),
        )
    await db.commit()
    return await get_message_run(
        db,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        client_message_id=client_message_id,
    )


async def add_message(
    db,
    *,
    message_id: str,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    role: str,
    content: str,
    status: str,
    citations: list[dict],
) -> dict:
    now = utc_now()
    await db.execute(
        """INSERT INTO ext_v2_message
           (message_id, conversation_id, tenant_id, business_user_id, role,
            content, status, citations_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            conversation_id,
            tenant_id,
            business_user_id,
            role,
            content,
            status,
            json.dumps(citations, ensure_ascii=False, separators=(",", ":")),
            now,
        ),
    )
    for citation in citations:
        await db.execute(
            """INSERT INTO ext_v2_citation
               (citation_id, message_id, conversation_id, tenant_id,
                business_user_id, snapshot_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(citation_id) DO NOTHING""",
            (
                citation["citationId"],
                message_id,
                conversation_id,
                tenant_id,
                business_user_id,
                json.dumps(citation, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )
    title_update = ""
    params: list[object] = [now]
    if role == "user":
        title = " ".join(content.split())[:80] or "New conversation"
        title_update = ", title=CASE WHEN title='New conversation' THEN ? ELSE title END"
        params.append(title)
    params.extend([conversation_id, tenant_id, business_user_id])
    await db.execute(
        f"""UPDATE ext_v2_conversation SET last_message_at=?{title_update}
            WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        tuple(params),
    )
    await db.commit()
    return {
        "messageId": message_id,
        "role": role,
        "content": content,
        "status": status,
        "citations": citations,
        "createdAt": now,
    }


async def list_messages(
    db,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None, bool]:
    marker = decode_cursor(cursor)
    where = "conversation_id=? AND tenant_id=? AND business_user_id=?"
    params: list[object] = [conversation_id, tenant_id, business_user_id]
    if marker:
        where += " AND (created_at > ? OR (created_at = ? AND message_id > ?))"
        params.extend([marker[0], marker[0], marker[1]])
    params.append(limit + 1)
    async with db.execute(
        f"""SELECT * FROM ext_v2_message WHERE {where}
            ORDER BY created_at ASC, message_id ASC LIMIT ?""",
        tuple(params),
    ) as db_cursor:
        rows = await db_cursor.fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = []
    for row in page:
        try:
            citations = json.loads(row["citations_json"] or "[]")
        except json.JSONDecodeError:
            citations = []
        items.append(
            {
                "messageId": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "status": row["status"],
                "citations": citations,
                "createdAt": row["created_at"],
            }
        )
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(page[-1]["created_at"], page[-1]["message_id"])
    return items, next_cursor, has_more


async def get_citation(
    db, *, citation_id: str, tenant_id: str, business_user_id: str
) -> dict | None:
    async with db.execute(
        """SELECT snapshot_json FROM ext_v2_citation
           WHERE citation_id=? AND tenant_id=? AND business_user_id=? LIMIT 1""",
        (citation_id, tenant_id, business_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["snapshot_json"])
    except json.JSONDecodeError:
        return None
