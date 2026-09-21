"""Service-free control-flow probes of the real store function (not PG tests)."""
import ast
import asyncio
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('transitioned', [None, {'assistant_message_id': 'synthetic-assistant'}, {'assistant_message_id': None}])
def test_only_expiry_winner_inserts_placeholder(transitioned):
    writes = []
    connection = object()

    async def fetch(conn, sql, params):
        assert conn is connection
        assert "status='running'" in sql and 'lease_expires_at::timestamptz <= clock_timestamp()' in sql
        assert 'RETURNING assistant_message_id' in sql
        return transitioned

    async def execute(conn, sql, params):
        assert conn is connection
        writes.append((sql, params))

    async def current(conn, **kwargs):
        return {'status': 'failed' if transitioned else 'running'}

    async def noop(*args, **kwargs):
        pass

    namespace = dict(lock_conversation=noop, quarantine_conversation=noop, utc_now=lambda: '2026-09-20T00:00:00Z', json=json,
                     fetchone=fetch, exec_sql=execute, get_message_run=current)
    path = Path(__file__).parents[1] / 'gateway/query/v2_store.py'
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'mark_expired_run_interrupted')
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
    result = asyncio.run(namespace['mark_expired_run_interrupted'](
        connection, conversation_id='c', tenant_id='t', business_user_id='u', client_message_id='m'))
    assert result['status'] == ('failed' if transitioned else 'running')
    if transitioned and transitioned['assistant_message_id']:
        assert len(writes) == 1
        assert 'INSERT INTO ext_v2_message' in writes[0][0]
        assert writes[0][1][0] == 'synthetic-assistant'
    else:
        assert writes == []
