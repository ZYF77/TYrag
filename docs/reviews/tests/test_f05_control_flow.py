"""Run real store functions without loading service configuration; not PG acceptance."""
import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]


def store_functions(namespace):
    namespace["__package__"] = "enterprise.gateway.query"
    tree = ast.parse((ROOT / 'enterprise/gateway/query/v2_store.py').read_text())
    names = {'complete_message_run', 'save_terminal_message_run', 'RunOwnershipLost'}
    nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<store>', 'exec'), namespace)
    return namespace


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_writer_cannot_save_message(self):
        writes = []
        async def lock(*a, **kw): pass
        async def update(conn, sql, params):
            self.assertIn('run_id=?', sql)
            self.assertIn('clock_timestamp()', sql)
            self.assertIn("status='running'", sql)
            return None
        async def add(*a, **kw): writes.append(kw)
        ns = store_functions(dict(json=json, lock_conversation=lock, fetchone=update, add_message=add))
        with self.assertRaises(ns['RunOwnershipLost']):
            await ns['save_terminal_message_run'](None, conversation_id='c', tenant_id='t', business_user_id='u',
                client_message_id='m', run_id='old', result={}, assistant_message_id='a', content='late',
                citations=[], business_status='completed')
        self.assertEqual(writes, [])

    async def test_projection_failure_propagates_for_transaction_rollback(self):
        order = []
        async def lock(*a, **kw): pass
        async def update(*a):
            order.append('cas')
            return {'run_id':'r'}
        async def add(*a, **kw):
            order.append('message')
            raise RuntimeError('synthetic failure')
        ns = store_functions(dict(json=json, lock_conversation=lock, fetchone=update, add_message=add))
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            await ns['save_terminal_message_run'](None, conversation_id='c', tenant_id='t', business_user_id='u',
                client_message_id='m', run_id='r', result={}, assistant_message_id='a', content='safe',
                citations=[], business_status='completed')
        self.assertEqual(order, ['cas', 'message'])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load the actual lifecycle module; only DB and transport dependencies
        # are synthetic. This exercises cancellation and async-generator exits.
        package = ModuleType('f05_fixture')
        package.__path__ = []
        store = ModuleType('f05_fixture.v2_store')
        store.RunOwnershipLost = type('RunOwnershipLost', (Exception,), {})
        store.assert_run_owner = AsyncMock()
        store.renew_run = AsyncMock()
        v2 = ModuleType('f05_fixture.v2_router')
        v2._sse = lambda event, data: f'event: {event}\ndata: {json.dumps(data)}\n\n'
        async def result_events(result):
            yield v2._sse('answer.completed', result)
        v2._result_events = result_events
        ops = ModuleType('enterprise.gateway.db.ops')
        async def write(db, fn, **kwargs):
            return await fn(db, **kwargs)
        ops.gw_write = write
        modules = {'f05_fixture': package, 'f05_fixture.v2_store': store,
                   'f05_fixture.v2_router': v2, 'enterprise.gateway.db.ops': ops}
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location('f05_fixture.run_lifecycle',
            ROOT / 'enterprise/gateway/query/run_lifecycle.py')
        self.lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lifecycle)
        self.store = store
        self.args = (None, SimpleNamespace(tenant_id='t', business_user_id='u'),
                     {'conversation_id': 'c'}, {'run_id': 'r', 'client_message_id': 'm'})

    async def test_cleanup_failure_after_terminal_does_not_emit_second_terminal(self):
        async def source(db, principal, conversation, run):
            yield 'event: answer.completed\ndata: {}\n\n'
            raise RuntimeError('synthetic cleanup error')
        recover = AsyncMock()
        with patch.object(self.lifecycle, 'interrupted_result', recover):
            events = [event async for event in self.lifecycle.leased_run(source)(*self.args)]
        self.assertEqual(len(events), 1)
        self.assertIn('answer.completed', events[0])
        recover.assert_not_called()

    async def test_error_before_terminal_replays_committed_success(self):
        async def source(db, principal, conversation, run):
            yield 'event: answer.delta\ndata: {}\n\n'
            raise RuntimeError('synthetic post-commit error')
        with patch.object(self.lifecycle, 'interrupted_result', AsyncMock(return_value=({'status': 'completed'}, None))):
            events = [event async for event in self.lifecycle.leased_run(source)(*self.args)]
        self.assertEqual(len(events), 2)
        self.assertIn('answer.completed', events[-1])
        self.assertFalse(any('run.failed' in event for event in events))

    async def test_committed_terminal_stops_heartbeat(self):
        with patch.object(self.lifecycle, 'HEARTBEAT_SECONDS', .002):
            async with self.lifecycle.execution_lease(*self.args):
                self.lifecycle.active_run.get()['terminal_committed'] = True
                await asyncio.sleep(.015)
        self.store.renew_run.assert_not_called()
