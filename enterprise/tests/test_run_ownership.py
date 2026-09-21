"""F05 PostgreSQL integration tests: real transactions, synthetic identities."""
import asyncio
import pytest
from enterprise.gateway.db.ops import gw_read, gw_write
from enterprise.gateway.db.dialect import fetchall, exec_sql
from enterprise.gateway.query import v2_store as store


@pytest.fixture
def identity():
    return dict(conversation_id='ownership', tenant_id='synthetic', business_user_id='u')


async def create(db, identity):
    await gw_write(db, store.create_conversation, **identity, equipment_id=None, fixed_asset_no=None, fault_code=None)


async def reserve(db, identity, key):
    return await gw_write(db, store.reserve_message_run, **identity, client_message_id=key,
        run_id=key, request_hash=key, user_message_id='u-'+key, assistant_message_id='a-'+key, question=key)


async def finish(db, identity, key):
    await gw_write(db, store.save_terminal_message_run, **identity, client_message_id=key,
        run_id=key, assistant_message_id='a-'+key, result={'answer':'safe','status':'completed'},
        content='safe', citations=[], business_status='completed')


@pytest.mark.asyncio
async def test_two_connections_one_active_run(isolated_gateway_db, identity):
    db, _ = isolated_gateway_db
    await create(db, identity)
    results = await asyncio.gather(reserve(db, identity, 'one'), reserve(db, identity, 'two'), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    loser = next(r for r in results if isinstance(r, Exception))
    assert isinstance(loser, store.ConversationUnavailable) and loser.code == 'CONVERSATION_BUSY'
    assert len(await gw_read(db, fetchall, "SELECT * FROM ext_v2_message WHERE role='user'")) == 1


@pytest.mark.asyncio
async def test_expiry_fences_completion_and_requires_new_conversation(isolated_gateway_db, identity):
    db, _ = isolated_gateway_db
    await create(db, identity)
    await reserve(db, identity, 'one')
    await gw_write(db, exec_sql, "UPDATE ext_v2_message_run SET lease_expires_at='2000-01-01T00:00:00Z'")
    await gw_write(db, store.expire_conversation_run, **identity)
    with pytest.raises(store.RunOwnershipLost):
        await finish(db, identity, 'one')
    with pytest.raises(store.ConversationUnavailable, match='CONVERSATION_RESTART_REQUIRED'):
        await reserve(db, identity, 'two')
    messages = await gw_read(db, fetchall, "SELECT * FROM ext_v2_message WHERE role='assistant'")
    assert len(messages) == 1 and messages[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_terminal_insert_failure_rolls_back(isolated_gateway_db, identity, monkeypatch):
    db, _ = isolated_gateway_db
    await create(db, identity)
    await reserve(db, identity, 'one')
    async def fail(*args, **kwargs):
        raise RuntimeError('synthetic insert failure')
    monkeypatch.setattr(store, 'add_message', fail)
    with pytest.raises(RuntimeError, match='synthetic insert'):
        await finish(db, identity, 'one')
    current = await gw_read(db, store.get_message_run, **identity, client_message_id='one')
    assert current['status'] == 'running' and current['result_json'] is None


@pytest.mark.asyncio
async def test_renewal_and_wrong_owner(isolated_gateway_db, identity):
    db, _ = isolated_gateway_db
    await create(db, identity)
    before = await reserve(db, identity, 'one')
    await gw_write(db, store.renew_run, **identity, run_id='one')
    after = await gw_read(db, store.get_message_run, **identity, client_message_id='one')
    assert after['lease_expires_at'] >= before['lease_expires_at']
    with pytest.raises(store.RunOwnershipLost):
        await gw_write(db, store.renew_run, **identity, run_id='other')
    await finish(db, identity, 'one')
    with pytest.raises(store.RunOwnershipLost):
        await finish(db, identity, 'one')
    await reserve(db, identity, 'two')
