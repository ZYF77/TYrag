"""Enterprise-owned conversation truth store for the v2 external API."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import begin_transaction, exec_sql, fetchall, fetchone

import base64
import json
from datetime import datetime, timezone


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



PUBLIC_STATUS = {
    "completed": "已完成",
    "no_reliable_evidence": "无可靠依据",
    "failed": "失败",
    "running": "处理中",
    "active": "进行中",
    "archived": "已归档",
}


def public_status(status: str | None) -> str:
    """Map stored English status codes to the v2 client-facing Chinese labels."""
    if not status:
        return ""
    return PUBLIC_STATUS.get(status, status)


def conversation_payload(row) -> dict:
    return {
        "conversationId": row["conversation_id"],
        "title": row["title"],
        "status": public_status(row["status"]),
        "equipmentId": row["equipment_id"],
        "fixedAssetNo": row["fixed_asset_no"],
        "faultCode": row["fault_code"],
        "contextVersion": row["context_version"],
        "lastMessageAt": row["last_message_at"],
        "createdAt": row["created_at"],
    }


async def create_conversation(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    fault_code: str | None,
    asset_id: str | None = None,
    registry_version: str | None = None,
    context_resolved_at: str | None = None,
) -> dict:
    now = utc_now()
    context_version = int(
        any(value is not None for value in (equipment_id, fixed_asset_no, fault_code))
    )
    result = await exec_sql(conn,
        """INSERT INTO ext_v2_conversation
           (conversation_id, tenant_id, business_user_id, title,
            equipment_id, fixed_asset_no, asset_id, fault_code, context_version,
            status, ragflow_chat_id, ragflow_session_id, registry_version,
            context_resolved_at, first_message_at, created_at, last_message_at)
           VALUES (?, ?, ?, 'New conversation', ?, ?, ?, ?, ?, 'active',
                   NULL, NULL, ?, ?, NULL, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            equipment_id,
            fixed_asset_no,
            asset_id,
            fault_code,
            context_version,
            registry_version,
            context_resolved_at,
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
    conn, *, conversation_id: str, tenant_id: str, business_user_id: str
) -> dict | None:
    row = await fetchone(conn, """SELECT * FROM ext_v2_conversation
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
           LIMIT 1""", (conversation_id, tenant_id, business_user_id))
    return dict(row) if row else None


async def update_conversation_mapping(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    ragflow_chat_id: str | None,
    ragflow_session_id: str | None,
) -> None:
    result = await exec_sql(conn,
        """UPDATE ext_v2_conversation
           SET ragflow_chat_id=COALESCE(?, ragflow_chat_id),
               ragflow_session_id=COALESCE(?, ragflow_session_id)
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (
            ragflow_chat_id,
            ragflow_session_id,
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )


async def list_conversations(
    conn,
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
    rows = await fetchall(
        conn,
        f"""SELECT * FROM ext_v2_conversation WHERE {where}
            ORDER BY last_message_at DESC, conversation_id DESC LIMIT ?""",
        tuple(params),
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(page[-1]["last_message_at"], page[-1]["conversation_id"])
    return [conversation_payload(row) for row in page], next_cursor, has_more


async def update_context(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    equipment_id: str | None,
    fixed_asset_no: str | None,
    fault_code: str | None,
    context_version: int,
    asset_id: str | None = None,
    registry_version: str | None = None,
    context_resolved_at: str | None = None,
    expected_context_version: int | None = None,
) -> dict | None:
    query = """UPDATE ext_v2_conversation
               SET equipment_id=?, fixed_asset_no=?, asset_id=?, fault_code=?,
                   context_version=?, registry_version=?, context_resolved_at=?
               WHERE conversation_id=? AND tenant_id=? AND business_user_id=?"""
    params: list[object] = [
        equipment_id,
        fixed_asset_no,
        asset_id,
        fault_code,
        context_version,
        registry_version,
        context_resolved_at,
        conversation_id,
        tenant_id,
        business_user_id,
    ]
    if expected_context_version is not None:
        query += " AND context_version=?"
        params.append(expected_context_version)
    result = await exec_sql(conn, query, tuple(params))
    if result.rowcount != 1:
        return None
    return await get_conversation(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def count_messages(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> int:
    row = await fetchone(conn, """SELECT COUNT(*) AS n FROM ext_v2_message
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""", (conversation_id, tenant_id, business_user_id))
    return int(row["n"] if row else 0)


async def list_messages_ordered(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
) -> list[dict]:
    row = await fetchone(conn, """SELECT message_id, role, content, status, created_at
           FROM ext_v2_message
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
           ORDER BY created_at ASC, message_id ASC""", (conversation_id, tenant_id, business_user_id))
    return [dict(row) for row in rows]


async def save_context_summary(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    context_summary: str,
    compressed_turn_watermark: int,
    clear_ragflow_session: bool = True,
) -> dict | None:
    now = utc_now()
    session_clause = ", ragflow_session_id=NULL" if clear_ragflow_session else ""
    result = await exec_sql(conn,
        f"""UPDATE ext_v2_conversation
           SET context_summary=?, summary_updated_at=?,
               compressed_turn_watermark=?{session_clause}
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (
            context_summary,
            now,
            int(compressed_turn_watermark),
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )
    return await get_conversation(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def archive_conversation(
    conn, *, conversation_id: str, tenant_id: str, business_user_id: str
) -> dict:
    result = await exec_sql(conn,
        """UPDATE ext_v2_conversation SET status='archived'
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (conversation_id, tenant_id, business_user_id),
    )
    return await get_conversation(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
    )


async def get_message_run(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
) -> dict | None:
    row = await fetchone(conn, """SELECT * FROM ext_v2_message_run
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? LIMIT 1""", (conversation_id, tenant_id, business_user_id, client_message_id))
    return dict(row) if row else None


async def list_recent_entity_scopes(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    limit: int = 2,
) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT entity_scope_json, created_at FROM ext_v2_message_run
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND entity_scope_json IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (conversation_id, tenant_id, business_user_id, max(1, limit)),
    )
    scopes: list[dict] = []
    for row in rows:
        try:
            values = json.loads(row["entity_scope_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            scope = [str(value) for value in values if str(value).strip()]
            if scope:
                scopes.append({"entity_ids": scope, "created_at": row["created_at"]})
    return scopes


async def reserve_message_run(
    conn,
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
    title: str | None = None,
    entity_scope: list[str] | tuple[str, ...] = (),
    allowed_doc_ids: list[str] | tuple[str, ...] = (),
    lease_seconds: int = 1800,
) -> dict | None:
    run_id = run_id or __import__("uuid").uuid4().hex
    lease_expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + lease_seconds,
        tz=timezone.utc,
    ).isoformat()
    await begin_transaction(conn)
    result = await exec_sql(conn,
        """INSERT INTO ext_v2_message_run
           (conversation_id, tenant_id, business_user_id, client_message_id,
             request_hash, run_id, status, lease_expires_at, user_message_id,
             assistant_message_id, result_json, entity_scope_json,
             allowed_doc_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?, ?)
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
            json.dumps(list(entity_scope), ensure_ascii=False, separators=(",", ":")),
            json.dumps(list(allowed_doc_ids), ensure_ascii=False, separators=(",", ":")),
            utc_now(),
        ),
    )
    if result.rowcount == 1 and question is not None and user_message_id:
        now = utc_now()
        result = await exec_sql(conn,
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
        title = title or (" ".join((question or "").split())[:80] or "New conversation")
        result = await exec_sql(conn,
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
    if result.rowcount != 1:
        return None
    return await get_message_run(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        client_message_id=client_message_id,
    )


async def complete_message_run(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
    result: dict,
    status: str = "completed",
    assistant_message_id: str | None = None,
) -> None:
    result = await exec_sql(conn,
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


async def mark_expired_run_interrupted(
    conn,
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
    result = await exec_sql(conn,
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
    row = await fetchone(
        conn,
        """SELECT assistant_message_id FROM ext_v2_message_run
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=?""",
        (conversation_id, tenant_id, business_user_id, client_message_id),
    )
    if row and row["assistant_message_id"]:
        result = await exec_sql(conn,
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
    return await get_message_run(
        conn,
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        business_user_id=business_user_id,
        client_message_id=client_message_id,
    )


async def add_message(
    conn,
    *,
    message_id: str,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    role: str,
    content: str,
    status: str,
    citations: list[dict],
    reasoning: str | None = None,
) -> dict:
    now = utc_now()
    result = await exec_sql(conn,
        """INSERT INTO ext_v2_message
           (message_id, conversation_id, tenant_id, business_user_id, role,
            content, status, citations_json, reasoning, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            conversation_id,
            tenant_id,
            business_user_id,
            role,
            content,
            status,
            json.dumps(citations, ensure_ascii=False, separators=(",", ":")),
            reasoning if role == "assistant" else None,
            now,
        ),
    )
    for citation in citations:
        result = await exec_sql(conn,
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
    result = await exec_sql(conn,
        f"""UPDATE ext_v2_conversation SET last_message_at=?{title_update}
            WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        tuple(params),
    )
    return {
        "messageId": message_id,
        "role": role,
        "content": content,
        "status": status,
        "citations": citations,
        "reasoning": reasoning if role == "assistant" else None,
        "createdAt": now,
    }


async def claim_ragflow_session(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    ragflow_chat_id: str,
    ragflow_session_id: str,
) -> int:
    """Atomically bind session when still unset; returns affected rowcount."""
    result = await exec_sql(
        conn,
        """UPDATE ext_v2_conversation
           SET ragflow_chat_id=?, ragflow_session_id=?
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND ragflow_session_id IS NULL""",
        (
            ragflow_chat_id,
            ragflow_session_id,
            conversation_id,
            tenant_id,
            business_user_id,
        ),
    )
    return int(result.rowcount or 0)


async def set_message_attachments(
    conn, *, message_id: str, attachments: list[dict]
) -> None:
    result = await exec_sql(conn,
        "UPDATE ext_v2_message SET attachments_json=? WHERE message_id=?",
        (json.dumps(attachments, ensure_ascii=False, separators=(",", ":")), message_id),
    )


async def list_messages(
    conn,
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
    rows = await fetchall(
        conn,
        f"""SELECT * FROM ext_v2_message WHERE {where}
            ORDER BY created_at ASC, message_id ASC LIMIT ?""",
        tuple(params),
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    items = []
    for row in page:
        try:
            citations = json.loads(row["citations_json"] or "[]")
        except json.JSONDecodeError:
            citations = []
        keys = set(row.keys())
        reasoning = None
        if "reasoning" in keys:
            value = row["reasoning"]
            reasoning = value if isinstance(value, str) and value.strip() else None
        items.append(
            {
                "messageId": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "status": public_status(row["status"]),
                "citations": citations,
                "reasoning": reasoning if row["role"] == "assistant" else None,
                "createdAt": row["created_at"],
            }
        )
        if "attachments_json" in keys:
            try:
                attachments = json.loads(row["attachments_json"] or "[]")
            except json.JSONDecodeError:
                attachments = []
            if attachments:
                items[-1]["attachments"] = attachments
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(page[-1]["created_at"], page[-1]["message_id"])
    return items, next_cursor, has_more


async def get_citation(
    conn, *, citation_id: str, tenant_id: str, business_user_id: str
) -> dict | None:
    row = await fetchone(
        conn,
        """SELECT snapshot_json FROM ext_v2_citation
           WHERE citation_id=? AND tenant_id=? AND business_user_id=? LIMIT 1""",
        (citation_id, tenant_id, business_user_id),
    )
    if not row:
        return None
    try:
        return json.loads(row["snapshot_json"])
    except json.JSONDecodeError:
        return None
