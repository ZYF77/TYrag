"""Real parent writer/reader with an in-memory document store; no service credentials."""
import ast
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ragflow'))
from rag.utils.parent_chunks import create_parent_chunks, expand_parents, parent_id, parent_doc_aggs


def child(doc='A', kb='kb', text='同一父段落'):
    return dict(id='child-'+doc, doc_id=doc, kb_id=kb, mom=text, content_with_weight='子段落',
                docnm_kwd=doc+'.txt', similarity=0.8, content_ltks='child', position_int=[[1, 2]])


class Store:
    def __init__(self, parents):
        self.parents = parents
        self.calls = []
    def search(self, fields, highlight, condition, expressions, order, offset, limit, index, kbs):
        self.calls.append((condition, index, kbs))
        return {p['id']: p for p in self.parents.get(index, [])
                if p['id'] in condition['id'] and p['doc_id'] in condition['doc_id'] and p['kb_id'] in condition['kb_id']}
    def get_fields(self, result, fields):
        return result


def expand(store, children, tenants=('t',)):
    return expand_parents(store, children, tenants, lambda t: 'index-'+t, lambda: None)


class ParentSourceTests(unittest.TestCase):
    def test_text_collision_is_namespaced(self):
        base = ['t', 'kb', 'A', 'task', 'same']
        first = parent_id(*base)
        self.assertEqual(first, parent_id(*base))
        for i in range(5):
            other = base.copy(); other[i] += '-different'
            self.assertNotEqual(first, parent_id(*other))

    def test_scope_and_title_come_from_same_document(self):
        children = [child('A'), child('B')]
        parents = create_parent_chunks(children, 't', 'kb', 'task')
        store = Store({'index-t': parents})
        result = expand(store, children[:1])
        self.assertEqual([c['doc_id'] for c in result], ['A'])
        self.assertEqual(result[0]['content_with_weight'], '同一父段落')
        self.assertEqual(result[0]['docnm_kwd'], 'A.txt')
        self.assertEqual(store.calls[0][0]['doc_id'], ['A'])
        self.assertEqual(parent_doc_aggs(result), [{'doc_id':'A', 'doc_name':'A.txt', 'count':1}])
        self.assertEqual({c['doc_id'] for c in expand(store, children)}, {'A','B'})

    def test_old_missing_invalid_and_corrupt_parent_keep_child(self):
        children = [child()]
        parents = create_parent_chunks(children, 't', 'kb', 'task')
        for mutation in ['missing','source','text','legacy']:
            c = copy.deepcopy(children); p = copy.deepcopy(parents)
            if mutation == 'missing': p = []
            if mutation == 'source': p[0]['doc_id'] = 'B'
            if mutation == 'text': p[0]['content_with_weight'] = 'corrupted'
            if mutation == 'legacy': c[0]['mom_id'] = '0123456789abcdef'
            self.assertEqual(expand(Store({'index-t':p}), c), c)

    def test_searches_authorized_tenant_not_just_first(self):
        children = [child()]
        parents = create_parent_chunks(children, 'second', 'kb', 'task')
        store = Store({'index-second': parents})
        self.assertEqual(expand(store, children, ['first','second'])[0]['chunk_id'], parents[0]['id'])
        self.assertEqual(expand(store, children, ['first']), children)

    def test_legacy_does_not_fetch_and_invalid_source_clears_pointer(self):
        c = child(); c['mom_id'] = 'old'
        store = Store({})
        self.assertEqual(expand(store, [c]), [c]); self.assertEqual(store.calls, [])
        self.assertEqual(create_parent_chunks([c], '', 'kb', 'task'), [])
        self.assertNotIn('mom_id', c)

    def test_reparse_and_retry(self):
        c = [child()]
        old = create_parent_chunks(c, 't','kb','old')
        new = create_parent_chunks(c, 't','kb','new')
        self.assertNotEqual(old[0]['id'], new[0]['id'])
        self.assertEqual(create_parent_chunks(c, 't','kb','new'), new)
        self.assertEqual(expand(Store({'index-t':old}), c), c)

    def test_refactor_writer_and_dealer_use_real_shared_methods(self):
        ns = {'List':list, 'Dict':dict}
        tree = ast.parse((ROOT/'ragflow/rag/svr/task_executor_refactor/chunk_service.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=='_create_mother_chunks')
        method.decorator_list=[]
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<writer>', 'exec'), ns)
        children=[child()]
        self.assertEqual(ns['_create_mother_chunks'](None, children,'t','kb','task'), create_parent_chunks([child()],'t','kb','task'))
        tree=ast.parse((ROOT/'ragflow/rag/nlp/search.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='retrieval_by_children')
        ns.update(index_name=lambda t:'index-'+t, OrderByExpr=lambda:None)
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<dealer>','exec'),ns)
        parents=create_parent_chunks(children,'t','kb','task')
        from types import SimpleNamespace
        result=ns['retrieval_by_children'](SimpleNamespace(dataStore=Store({'index-t':parents})), children,['t'])
        self.assertEqual(result[0]['chunk_id'], parents[0]['id'])
