"""Bounded at-least-once mirror; the confirmed local ledger remains authoritative."""
import asyncio
import hashlib
import uuid
from types import SimpleNamespace
from enterprise.gateway.db.dialect import fetchone, exec_sql
from .memory_subject import memory_subject_from_principal
from . import user_memory
from .preference_rules import VALUES


async def claim(conn):
    await exec_sql(conn, """UPDATE ext_preference_outbox SET status='dead',claim_id=NULL
        WHERE attempts>=8 AND status='delivering' AND lease_until::timestamptz<clock_timestamp()""")
    row = await fetchone(conn, """SELECT * FROM ext_preference_outbox o
        WHERE (status='pending' AND next_attempt_at::timestamptz<=clock_timestamp()
            OR status='delivering' AND lease_until::timestamptz<clock_timestamp())
        AND NOT EXISTS (SELECT 1 FROM ext_preference_outbox older
            WHERE older.tenant_id=o.tenant_id AND older.business_user_id=o.business_user_id
            AND older.preference_key=o.preference_key AND older.revision<o.revision
            AND older.status IN ('pending','delivering'))
        ORDER BY revision, event_id FOR UPDATE SKIP LOCKED LIMIT 1""")
    if not row:
        return None
    row = dict(row)
    row["claim_id"] = uuid.uuid4().hex
    await exec_sql(conn, """UPDATE ext_preference_outbox SET status='delivering',claim_id=?,
        attempts=attempts+1, lease_until=(clock_timestamp()+interval '60 seconds')::text WHERE event_id=?""",
        (row["claim_id"], row["event_id"]))
    row["attempts"] += 1
    return row


class PreferenceWorker:
    def __init__(self, db):
        self.db = db

    async def tick(self):
        if not user_memory.memory_config_ready():
            return False
        async with self.db.transaction(write=True) as conn:
            event = await claim(conn)
        if not event:
            return False
        status = 'delivered'
        try:
            async with asyncio.timeout(30):
                async with self.db.transaction(write=False) as conn:
                    current = await fetchone(conn, """SELECT revision FROM ext_user_preference
                        WHERE tenant_id=? AND business_user_id=? AND preference_key=?""",
                        (event['tenant_id'],event['business_user_id'],event['preference_key']))
                if not current or current['revision'] != event['revision']:
                    status = 'superseded'
                else:
                    await self.deliver(event)
        except Exception:
            status = 'dead' if event['attempts'] >= 8 else 'pending'
        async with self.db.transaction(write=True) as conn:
            await exec_sql(conn, """UPDATE ext_preference_outbox SET status=?, lease_until=NULL,
                next_attempt_at=(clock_timestamp()+(? * interval '1 second'))::text
                WHERE event_id=? AND claim_id=? AND status='delivering'""",
                (status, min(3600, 15 * 2 ** event['attempts']),event['event_id'],event['claim_id']))
        return True

    async def deliver(self, event):
        client = user_memory.get_memory_client()
        client.timeout = min(10, user_memory.user_memory_timeout_seconds())
        subject = memory_subject_from_principal(SimpleNamespace(**event))
        agent = 'confirmed-preference-v1:' + hashlib.sha256(subject.encode()).hexdigest()[:24] + ':' + event['preference_key']
        # Purge earlier copies (including retries); only own namespace/subject.
        for _ in range(10):
            copies = await client.list_messages(memory_id=event['memory_id'], user_id=subject,
                agent_id=agent, limit=100)
            if not copies:
                break
            for item in copies:
                if item.get('agent_id') != agent or item.get('user_id') != subject:
                    raise ValueError('MIRROR_SCOPE_MISMATCH')
                await client.forget_message(memory_id=event['memory_id'], message_id=item['message_id'])
        else:
            raise ValueError('MIRROR_CLEANUP_BUDGET')
        if event['preference_value'] is not None:
            await client.add_message(memory_id=event['memory_id'], agent_id=agent,
                session_id=event['event_id'], user_id=subject,
                user_input=VALUES[event['preference_key']][event['preference_value']],
                agent_response='用户已确认的表达偏好；不作为事实证据。', request_id=event['event_id'])

    async def run_forever(self):
        while True:
            try:
                worked = await self.tick()
            except Exception:
                worked = False
            await asyncio.sleep(1 if worked else 10)
