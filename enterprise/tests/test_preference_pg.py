"""F11 PostgreSQL acceptance: real independent connections; synthetic preferences."""
import asyncio
import pytest
from enterprise.gateway.query import preference_store as store
from enterprise.gateway.db.dialect import fetchone

IDENTITY = dict(tenant_id='synthetic-t', business_user_id='synthetic-u')


async def candidate(db, run='r'):
    async with db.transaction(write=True) as conn:
        await store.capture(conn, **IDENTITY,run_id=run,conversation_id='synthetic-c',question='以后请简短回答')
        state=await store.list_state(conn,**IDENTITY)
        return state['candidates'][0]


@pytest.mark.asyncio
async def test_two_connections_confirm_once_and_delete(gateway_db):
    item=await candidate(gateway_db)
    async def confirm():
        async with gateway_db.transaction(write=True) as conn:
            return await store.decide(conn,**IDENTITY,candidate_id=item['id'],revision=0,confirm=True,memory_id='synthetic-pool')
    await asyncio.gather(confirm(),confirm())
    async with gateway_db.transaction(write=True) as conn:
        row=await fetchone(conn,'SELECT COUNT(*) AS n FROM ext_preference_outbox',())
        assert row['n']==1
        assert (await store.list_state(conn,**IDENTITY))['preferences'][0]['revision']==1
        await store.delete(conn,**IDENTITY,key='detail',revision=1,memory_id='synthetic-pool')
        assert (await store.list_state(conn,**IDENTITY))['preferences']==[]


@pytest.mark.asyncio
async def test_confirmation_rolls_back_with_outbox_and_owner_isolated(gateway_db):
    item=await candidate(gateway_db)
    with pytest.raises(RuntimeError,match='synthetic rollback'):
        async with gateway_db.transaction(write=True) as conn:
            await store.decide(conn,**IDENTITY,candidate_id=item['id'],revision=0,confirm=True,memory_id='synthetic-pool')
            raise RuntimeError('synthetic rollback')
    async with gateway_db.transaction(write=False) as conn:
        state=await store.list_state(conn,**IDENTITY)
        assert state['preferences']==[] and len(state['candidates'])==1
        assert (await fetchone(conn,'SELECT COUNT(*) AS n FROM ext_preference_outbox',()))['n']==0
        other=await store.list_state(conn,tenant_id='other-t',business_user_id='synthetic-u')
        assert other=={'preferences':[],'candidates':[]}
        with pytest.raises(ValueError,match='NOT_FOUND'):
            await store.decide(conn,tenant_id='other-t',business_user_id='synthetic-u',candidate_id=item['id'],
                revision=0,confirm=True,memory_id='synthetic-pool')


@pytest.mark.asyncio
async def test_capture_dedup_and_stale_revision(gateway_db):
    item=await candidate(gateway_db)
    await candidate(gateway_db)
    async with gateway_db.transaction(write=True) as conn:
        assert len((await store.list_state(conn,**IDENTITY))['candidates'])==1
        await store.decide(conn,**IDENTITY,candidate_id=item['id'],revision=0,confirm=True,memory_id='synthetic-pool')
    with pytest.raises(ValueError,match='REVISION_CONFLICT'):
        async with gateway_db.transaction(write=True) as conn:
            await store.delete(conn,**IDENTITY,key='detail',revision=0,memory_id='synthetic-pool')


@pytest.mark.asyncio
async def test_two_workers_claim_one_event_and_expired_claim_recovers(gateway_db):
    from enterprise.gateway.query.preference_worker import claim
    from enterprise.gateway.db.dialect import exec_sql
    item=await candidate(gateway_db)
    async with gateway_db.transaction(write=True) as conn:
        await store.decide(conn,**IDENTITY,candidate_id=item['id'],revision=0,confirm=True,memory_id='synthetic-pool')
    async def take():
        async with gateway_db.transaction(write=True) as conn:
            return await claim(conn)
    claims=await asyncio.gather(take(),take())
    assert sum(value is not None for value in claims)==1
    first=next(value for value in claims if value)
    async with gateway_db.transaction(write=True) as conn:
        await exec_sql(conn,"UPDATE ext_preference_outbox SET lease_until=(clock_timestamp()-interval '1 second')::text WHERE event_id=?",(first['event_id'],))
    recovered=await take()
    assert recovered['event_id']==first['event_id']
    assert recovered['claim_id']!=first['claim_id']
    assert recovered['attempts']==2
