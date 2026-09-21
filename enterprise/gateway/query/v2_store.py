"""Enterprise-owned conversation truth store for the v2 external API."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import begin_transaction, exec_sql, fetchall, fetchone

import base64
import json
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()



def encode_conversation_devices(devices: list[str] | tuple[str, ...] | None) -> str:
    values: list[str] = []
    for item in devices or ():
        value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def decode_conversation_devices(raw) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            values = []
    else:
        values = []
    if not isinstance(values, list):
        return []
    devices: list[str] = []
    for item in values:
        value = str(item or "").strip()
        if value and value not in devices:
            devices.append(value)
    return devices


def devices_from_row(row) -> list[str]:
    devices = decode_conversation_devices(row.get("conversation_devices") if hasattr(row, "get") else None)
    if devices:
        return devices
    equipment_id = row["equipment_id"] if "equipment_id" in row.keys() else None
    return [equipment_id] if equipment_id else []


_BUSINESS_CONTEXT_KEYS = (
    "equipment_id",
    "fixed_asset_no",
    "fault_code",
    "model",
    "equipment_type",
    "manufacturer",
)


def normalize_business_context(context: dict | None) -> dict:
    """Keep only the bounded, user-visible soft context fields."""
    if not isinstance(context, dict):
        return {}
    normalized = {"schema_version": 1}
    for key in _BUSINESS_CONTEXT_KEYS:
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    return normalized if len(normalized) > 1 else {}


def encode_business_context(context: dict | None) -> str:
    return json.dumps(
        normalize_business_context(context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_business_context(raw) -> dict:
    if isinstance(raw, dict):
        return normalize_business_context(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return normalize_business_context(value)


def business_context_from_row(row) -> dict:
    raw = row.get("business_context_json") if hasattr(row, "get") else None
    return decode_business_context(raw)


_UNSET = object()


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
    devices = devices_from_row(row)
    business_context = business_context_from_row(row)
    anchor = None
    if "anchor_equipment_id" in row.keys():
        anchor = row["anchor_equipment_id"]
    return {
        "conversationId": row["conversation_id"],
        "title": row["title"],
        "status": public_status(row["status"]),
        "equipmentId": row["equipment_id"],
        "fixedAssetNo": row["fixed_asset_no"],
        "faultCode": row["fault_code"],
        "model": business_context.get("model"),
        "equipmentType": business_context.get("equipment_type"),
        "manufacturer": business_context.get("manufacturer"),
        "conversationDevices": devices,
        "anchorEquipmentId": anchor,
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
    conversation_devices: list[str] | tuple[str, ...] | None = None,
    anchor_equipment_id: str | None = None,
    business_context: dict | None = None,
) -> dict:
    now = utc_now()
    context_version = int(
        any(value is not None for value in (equipment_id, fixed_asset_no, fault_code))
        or bool(normalize_business_context(business_context))
    )
    if conversation_devices is None:
        devices = [equipment_id] if equipment_id else []
    else:
        devices = list(conversation_devices)
    if anchor_equipment_id is None and equipment_id:
        # Create-with-equipment => scoped; create-time device is the durable anchor.
        anchor_equipment_id = equipment_id
    devices_json = encode_conversation_devices(devices)
    result = await exec_sql(conn,
        """INSERT INTO ext_v2_conversation
           (conversation_id, tenant_id, business_user_id, title,
            equipment_id, fixed_asset_no, asset_id, fault_code,
            business_context_json, conversation_devices, anchor_equipment_id, context_version,
            status, ragflow_chat_id, ragflow_session_id, registry_version,
            context_resolved_at, first_message_at, created_at, last_message_at)
           VALUES (?, ?, ?, 'New conversation', ?, ?, ?, ?, ?, ?, ?, ?, 'active',
                   NULL, NULL, ?, ?, NULL, ?, ?)""",
        (
            conversation_id,
            tenant_id,
            business_user_id,
            equipment_id,
            fixed_asset_no,
            asset_id,
            fault_code,
            encode_business_context(business_context),
            devices_json,
            anchor_equipment_id,
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
    await assert_conversation_writable(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id)
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


async def set_workflow_session(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    agent_id: str,
    version: str,
    session_id: str,
) -> None:
    """Bind one conversation to the configured Workflow agent/session.

    The binding is deliberately separate from the Chat ``ragflow_session_id``
    column. A test run can therefore switch between the two entry points without
    accidentally continuing the other runtime's conversation.
    """
    await assert_conversation_writable(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id)
    await exec_sql(
        conn,
        """UPDATE ext_v2_conversation
           SET workflow_agent_id=?, workflow_version=?, workflow_session_id=?
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (
            agent_id,
            version,
            session_id,
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
    conversation_devices: list[str] | tuple[str, ...] | None = None,
    update_devices: bool = False,
    anchor_equipment_id: str | None = None,
    update_anchor: bool = False,
    business_context: dict | None | object = _UNSET,
) -> dict | None:
    await assert_conversation_writable(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id)
    query = """UPDATE ext_v2_conversation
               SET equipment_id=?, fixed_asset_no=?, asset_id=?, fault_code=?,
                   context_version=?, registry_version=?, context_resolved_at=?"""
    params: list[object] = [
        equipment_id,
        fixed_asset_no,
        asset_id,
        fault_code,
        context_version,
        registry_version,
        context_resolved_at,
    ]
    if update_devices:
        query += ", conversation_devices=?"
        params.append(encode_conversation_devices(conversation_devices))
    if update_anchor:
        query += ", anchor_equipment_id=?"
        params.append(anchor_equipment_id)
    if business_context is not _UNSET:
        query += ", business_context_json=?"
        params.append(encode_business_context(business_context if isinstance(business_context, dict) else None))
    query += " WHERE conversation_id=? AND tenant_id=? AND business_user_id=?"
    params.extend(
        [
            conversation_id,
            tenant_id,
            business_user_id,
        ]
    )
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



async def archive_conversation(
    conn, *, conversation_id: str, tenant_id: str, business_user_id: str
) -> dict:
    await assert_conversation_writable(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id)
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
    retrieval_context: dict | None = None,
    lease_seconds: int = 120,
) -> dict | None:
    run_id = run_id or __import__("uuid").uuid4().hex
    await begin_transaction(conn)
    identity = dict(conversation_id=conversation_id, tenant_id=tenant_id,
                    business_user_id=business_user_id)
    await lock_conversation(conn, **identity)
    existing = await get_message_run(conn, **identity, client_message_id=client_message_id)
    if existing:
        return None
    await assert_conversation_writable(conn, **identity)
    clock = await fetchone(conn, "SELECT (clock_timestamp() + (? * interval '1 second'))::text AS expires", (lease_seconds,))
    lease_expires_at = clock["expires"]
    result = await exec_sql(conn,
        """INSERT INTO ext_v2_message_run
           (conversation_id, tenant_id, business_user_id, client_message_id,
             request_hash, run_id, status, lease_expires_at, user_message_id,
             assistant_message_id, result_json, entity_scope_json,
             allowed_doc_ids_json, retrieval_context_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?, ?, ?)
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
            json.dumps(retrieval_context or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
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
    conn, *, conversation_id: str, tenant_id: str, business_user_id: str,
    client_message_id: str, run_id: str, result: dict,
    status: str = "completed", assistant_message_id: str | None = None,
) -> None:
    identity = dict(conversation_id=conversation_id, tenant_id=tenant_id,
                    business_user_id=business_user_id)
    await lock_conversation(conn, **identity)
    row = await fetchone(conn,
        """UPDATE ext_v2_message_run
           SET result_json=?, status=?, assistant_message_id=?, lease_expires_at=NULL
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? AND run_id=? AND status='running'
             AND lease_expires_at::timestamptz > clock_timestamp()
           RETURNING run_id""",
        (json.dumps(result, ensure_ascii=False, separators=(",", ":")), status,
         assistant_message_id, conversation_id, tenant_id, business_user_id,
         client_message_id, run_id))
    if not row:
        raise RunOwnershipLost()


async def save_terminal_message_run(
    conn, *, conversation_id: str, tenant_id: str, business_user_id: str,
    client_message_id: str, run_id: str, result: dict, assistant_message_id: str,
    content: str, citations: list[dict], business_status: str,
    reasoning: str | None = None, workflow_binding: dict | None = None,
    restart_required: bool = False,
) -> None:
    """CAS first; all durable output and bindings share the caller's transaction."""
    identity = dict(conversation_id=conversation_id, tenant_id=tenant_id,
                    business_user_id=business_user_id)
    await complete_message_run(conn, **identity, client_message_id=client_message_id,
        run_id=run_id, result=result, status="failed" if business_status == "failed" else "completed",
        assistant_message_id=assistant_message_id)
    await add_message(conn, **identity, message_id=assistant_message_id, role="assistant",
        content=content, status=business_status, citations=citations, reasoning=reasoning)
    if workflow_binding:
        await exec_sql(conn, """UPDATE ext_v2_conversation
            SET workflow_agent_id=?, workflow_version=?, workflow_session_id=?
            WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
            (workflow_binding["agent_id"], workflow_binding["version"], workflow_binding["session_id"],
             conversation_id, tenant_id, business_user_id))
    if restart_required:
        await quarantine_conversation(conn, **identity)


async def mark_expired_run_interrupted(
    conn,
    *,
    conversation_id: str,
    tenant_id: str,
    business_user_id: str,
    client_message_id: str,
) -> dict | None:
    """Turn an abandoned run into a stable, replayable failure."""
    await lock_conversation(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id)
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
    transitioned = await fetchone(conn,
        """UPDATE ext_v2_message_run
           SET status='failed', result_json=?, lease_expires_at=NULL
           WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
             AND client_message_id=? AND status='running'
             AND lease_expires_at IS NOT NULL AND lease_expires_at::timestamptz <= clock_timestamp()
           RETURNING assistant_message_id""",
        (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            conversation_id,
            tenant_id,
            business_user_id,
            client_message_id,
        ),
    )
    if transitioned:
        await quarantine_conversation(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id)
    # Only the request that changed the run from expired ``running`` to
    # ``failed`` may create the replay placeholder.  A live duplicate gets no
    # write at all, and a concurrent loser observes the already-terminal run
    # below.  UPDATE ... RETURNING keeps this decision inside the same write
    # transaction as the placeholder insert.
    if transitioned and transitioned["assistant_message_id"]:
        result = await exec_sql(conn,
            """INSERT INTO ext_v2_message
               (message_id, conversation_id, tenant_id, business_user_id, role,
                content, status, citations_json, created_at)
               VALUES (?, ?, ?, ?, 'assistant', '', 'failed', '[]', ?)
               ON CONFLICT(message_id) DO NOTHING""",
            (
                transitioned["assistant_message_id"],
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


async def save_failed_message_run(
    conn, *, message_id: str, conversation_id: str, tenant_id: str,
    business_user_id: str, client_message_id: str, run_id: str, result: dict,
    content: str = "", citations: list[dict] | None = None,
    reasoning: str | None = None, restart_required: bool = False,
) -> None:
    await save_terminal_message_run(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id,
        client_message_id=client_message_id, run_id=run_id, result=result,
        assistant_message_id=message_id, content=content, citations=citations or [],
        business_status="failed", reasoning=reasoning, restart_required=restart_required)


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
    await assert_conversation_writable(conn, conversation_id=conversation_id,
        tenant_id=tenant_id, business_user_id=business_user_id)
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


class ConversationUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RunOwnershipLost(Exception):
    pass


async def lock_conversation(conn, *, conversation_id, tenant_id, business_user_id):
    row = await fetchone(conn, """SELECT * FROM ext_v2_conversation
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=? FOR UPDATE""",
        (conversation_id, tenant_id, business_user_id))
    if not row:
        raise ConversationUnavailable("CONVERSATION_NOT_FOUND")
    return row


async def quarantine_conversation(conn, *, conversation_id, tenant_id, business_user_id):
    await exec_sql(conn, """UPDATE ext_v2_conversation SET restart_required=1
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (conversation_id, tenant_id, business_user_id))


async def assert_conversation_writable(conn, *, conversation_id, tenant_id, business_user_id):
    from enterprise.gateway.query.run_lifecycle import active_run
    identity = dict(conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id)
    row = await lock_conversation(conn, **identity)
    if row.get("restart_required"):
        raise ConversationUnavailable("CONVERSATION_RESTART_REQUIRED")
    if row["status"] == "archived":
        raise ConversationUnavailable("CONVERSATION_ARCHIVED")
    owner = active_run.get()
    run = await fetchone(conn, """SELECT * FROM ext_v2_message_run
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=? AND status='running'""",
        (conversation_id, tenant_id, business_user_id))
    if owner and owner["identity"] == identity:
        await assert_run_owner(conn, **identity, run_id=owner["run_id"])
    elif run:
        # This transition may quarantine; callers must commit before returning 409.
        raise ConversationUnavailable("CONVERSATION_BUSY")


async def assert_run_owner(conn, *, conversation_id, tenant_id, business_user_id, run_id):
    await lock_conversation(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id)
    row = await fetchone(conn, """SELECT run_id FROM ext_v2_message_run
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=? AND run_id=?
          AND status='running' AND lease_expires_at::timestamptz > clock_timestamp() FOR UPDATE""",
        (conversation_id, tenant_id, business_user_id, run_id))
    if not row:
        raise RunOwnershipLost()


async def renew_run(conn, *, conversation_id, tenant_id, business_user_id, run_id):
    await assert_run_owner(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id, run_id=run_id)
    await exec_sql(conn, """UPDATE ext_v2_message_run
        SET lease_expires_at=(clock_timestamp() + interval '120 seconds')::text
        WHERE run_id=? AND conversation_id=? AND tenant_id=? AND business_user_id=?""",
        (run_id, conversation_id, tenant_id, business_user_id))


async def save_run_scope(conn, *, conversation_id, tenant_id, business_user_id, run_id,
                         entity_scope, allowed_doc_ids, retrieval_context):
    await assert_run_owner(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id, run_id=run_id)
    await exec_sql(conn, """UPDATE ext_v2_message_run SET entity_scope_json=?,
        allowed_doc_ids_json=?, retrieval_context_json=?
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=? AND run_id=?""",
        (json.dumps(entity_scope), json.dumps(allowed_doc_ids), json.dumps(retrieval_context),
         conversation_id, tenant_id, business_user_id, run_id))


async def expire_conversation_run(conn, *, conversation_id, tenant_id, business_user_id):
    await lock_conversation(conn, conversation_id=conversation_id, tenant_id=tenant_id, business_user_id=business_user_id)
    row = await fetchone(conn, """SELECT client_message_id FROM ext_v2_message_run
        WHERE conversation_id=? AND tenant_id=? AND business_user_id=? AND status='running'""",
        (conversation_id, tenant_id, business_user_id))
    if row:
        await mark_expired_run_interrupted(conn, conversation_id=conversation_id, tenant_id=tenant_id,
            business_user_id=business_user_id, client_message_id=row["client_message_id"])
