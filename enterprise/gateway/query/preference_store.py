"""Transactional preference confirmation and durable mirror delivery."""
import uuid
from enterprise.gateway.db.dialect import exec_sql, fetchone, fetchall
from .preference_rules import extract_preferences, VALUES


async def capture(conn, *, tenant_id, business_user_id, run_id, conversation_id, question):
    for key, value in extract_preferences(question).items():
        current = await fetchone(conn, """SELECT revision FROM ext_user_preference
            WHERE tenant_id=? AND business_user_id=? AND preference_key=?""", (tenant_id, business_user_id, key))
        await exec_sql(conn, """INSERT INTO ext_preference_candidate
            (candidate_id, tenant_id, business_user_id, run_id, conversation_id, preference_key,
             preference_value, expected_revision, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', (clock_timestamp()+interval '7 days')::text)
            ON CONFLICT(tenant_id, business_user_id, run_id, preference_key) DO NOTHING""",
            (uuid.uuid4().hex, tenant_id, business_user_id, run_id, conversation_id,
             key, value, current["revision"] if current else 0))


async def list_state(conn, *, tenant_id, business_user_id, conversation_id=None):
    identity = (tenant_id, business_user_id)
    prefs = await fetchall(conn, """SELECT preference_key AS key, preference_value AS value, revision
        FROM ext_user_preference WHERE tenant_id=? AND business_user_id=? AND preference_value IS NOT NULL""", identity)
    where = "tenant_id=? AND business_user_id=? AND status='pending' AND expires_at::timestamptz > clock_timestamp()"
    params = identity
    if conversation_id is not None:
        where += " AND conversation_id=?"
        params += (conversation_id,)
    candidates = await fetchall(conn, f"""SELECT candidate_id AS id, preference_key AS key,
        preference_value AS value, expected_revision AS revision, conversation_id
        FROM ext_preference_candidate WHERE {where}
        ORDER BY expires_at DESC LIMIT 100""", params)
    return {"preferences": [dict(x) for x in prefs], "candidates": [dict(x) for x in candidates]}



async def _change(conn, *, tenant_id, business_user_id, key, value, revision, memory_id):
    if key not in VALUES or (value is not None and value not in VALUES[key]):
        raise ValueError("INVALID_PREFERENCE")
    identity = (tenant_id, business_user_id, key)
    await exec_sql(conn, """INSERT INTO ext_user_preference
        (tenant_id,business_user_id,preference_key,preference_value,revision)
        VALUES (?,?,?,NULL,0) ON CONFLICT DO NOTHING""", identity)
    changed = await fetchone(conn, """UPDATE ext_user_preference SET preference_value=?, revision=revision+1
        WHERE tenant_id=? AND business_user_id=? AND preference_key=? AND revision=? RETURNING revision""",
        (value, *identity, revision))
    if not changed:
        raise ValueError("PREFERENCE_REVISION_CONFLICT")
    # Clean every pool previously used, even after configuration changes.
    pools = {memory_id} if memory_id else set()
    previous = await fetchall(conn, """SELECT DISTINCT memory_id FROM ext_preference_outbox
        WHERE tenant_id=? AND business_user_id=? AND preference_key=?""", identity)
    pools.update(row["memory_id"] for row in previous if row["memory_id"])
    for pool in sorted(pool for pool in pools if pool):
        await exec_sql(conn, """INSERT INTO ext_preference_outbox
            (event_id,tenant_id,business_user_id,preference_key,preference_value,revision,memory_id,
             status,attempts,next_attempt_at) VALUES (?,?,?,?,?,?,?,'pending',0,clock_timestamp()::text)""",
            (uuid.uuid4().hex, *identity, value if pool == memory_id else None, changed["revision"], pool))
    return {"key": key, "value": value, "revision": changed["revision"]}


async def decide(conn, *, tenant_id, business_user_id, candidate_id, revision, confirm, memory_id):
    row = await fetchone(conn, """SELECT * FROM ext_preference_candidate
        WHERE candidate_id=? AND tenant_id=? AND business_user_id=? FOR UPDATE""",
        (candidate_id, tenant_id, business_user_id))
    if not row:
        raise ValueError("PREFERENCE_NOT_FOUND")
    if row["status"] != "pending":
        if row["expected_revision"] == revision and row["status"] == ("confirmed" if confirm else "ignored"):
            return {"status": row["status"]}
        raise ValueError("PREFERENCE_REVISION_CONFLICT")
    valid = await fetchone(conn, "SELECT CAST(? AS timestamptz) > clock_timestamp() AS valid", (row["expires_at"],))
    if not valid["valid"] or row["expected_revision"] != revision:
        raise ValueError("PREFERENCE_REVISION_CONFLICT")
    if confirm:
        await _change(conn, tenant_id=tenant_id, business_user_id=business_user_id,
            key=row["preference_key"], value=row["preference_value"], revision=revision, memory_id=memory_id)
    status = "confirmed" if confirm else "ignored"
    await exec_sql(conn, "UPDATE ext_preference_candidate SET status=? WHERE candidate_id=?", (status,candidate_id))
    return {"status": status}


async def delete(conn, *, tenant_id, business_user_id, key, revision, memory_id):
    return await _change(conn, tenant_id=tenant_id, business_user_id=business_user_id,
        key=key, value=None, revision=revision, memory_id=memory_id)
