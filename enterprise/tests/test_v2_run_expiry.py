"""F01 PostgreSQL transaction and independent-connection regressions."""
import asyncio

import pytest
import pytest_asyncio

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.ops import gw_read, gw_write
from enterprise.gateway.query import v2_store


@pytest_asyncio.fixture
async def run_db(isolated_gateway_db):
    db, _ = isolated_gateway_db
    identity = dict(conversation_id='synthetic-f01', tenant_id='tenant', business_user_id='user')
    await gw_write(db, v2_store.create_conversation, **identity,
                   equipment_id=None, fixed_asset_no=None, fault_code=None)
    await gw_write(db, v2_store.reserve_message_run, **identity,
                   client_message_id='client', request_hash='synthetic',
                   assistant_message_id='assistant', user_message_id='user-message',
                   question='synthetic question', lease_seconds=-1)
    return db, dict(**identity, client_message_id='client')


@pytest.mark.asyncio
async def test_independent_connections_only_one_expiry_winner(run_db, monkeypatch):
    db, identity = run_db
    original = v2_store.fetchone
    winners = []
    ready = asyncio.Event()
    connections = []

    async def observe(conn, sql, params):
        row = await original(conn, sql, params)
        if sql.lstrip().startswith('UPDATE ext_v2_message_run'):
            winners.append(row is not None)
        return row

    monkeypatch.setattr(v2_store, 'fetchone', observe)

    async def attempt():
        async with db.transaction(write=True) as conn:
            # Force two distinct open PG connections before either UPDATE.
            connections.append(await fetchone(conn, 'SELECT pg_backend_pid() AS pid'))
            if len(connections) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
            return await v2_store.mark_expired_run_interrupted(conn, **identity)

    first, second = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), timeout=20)
    assert connections[0]['pid'] != connections[1]['pid']
    assert sorted(winners) == [False, True]
    assert first['status'] == second['status'] == 'failed'
    assert first['result_json'] == second['result_json']
    rows = await gw_read(db, fetchall, "SELECT * FROM ext_v2_message WHERE role='assistant'")
    assert len(rows) == 1
    assert rows[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_placeholder_failure_rolls_back_expiry_transition(run_db, monkeypatch):
    db, identity = run_db
    original = v2_store.exec_sql

    async def fail_insert(conn, sql, params):
        if 'INSERT INTO ext_v2_message' in sql:
            raise RuntimeError('synthetic insert failure')
        return await original(conn, sql, params)

    monkeypatch.setattr(v2_store, 'exec_sql', fail_insert)
    with pytest.raises(RuntimeError, match='synthetic insert failure'):
        await gw_write(db, v2_store.mark_expired_run_interrupted, **identity)
    run = await gw_read(db, v2_store.get_message_run, **identity)
    assert run['status'] == 'running'
    assert run['result_json'] is None
    assert run['lease_expires_at'] is not None
    assert await gw_read(db, fetchall, "SELECT * FROM ext_v2_message WHERE role='assistant'") == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'no_reliable_evidence', 'failed'])
async def test_terminal_run_is_unchanged(run_db, status):
    db, identity = run_db
    await gw_write(db, v2_store.complete_message_run, **identity,
                   result={'synthetic': status}, status=status, assistant_message_id='assistant')
    # Even a stale expired lease on a terminal row cannot reopen its outcome.
    await gw_write(db, exec_sql,
                   "UPDATE ext_v2_message_run SET lease_expires_at=? WHERE conversation_id=?",
                   ('2000-01-01T00:00:00+00:00', identity['conversation_id']))
    before = await gw_read(db, v2_store.get_message_run, **identity)
    after = await gw_write(db, v2_store.mark_expired_run_interrupted, **identity)
    assert after == before
    assert await gw_read(db, fetchall, "SELECT * FROM ext_v2_message WHERE role='assistant'") == []
