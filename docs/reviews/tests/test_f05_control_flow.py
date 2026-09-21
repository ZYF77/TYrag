"""Run real store functions without loading service configuration; not PG acceptance."""
import ast
import asyncio
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]


def store_functions(namespace):
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
