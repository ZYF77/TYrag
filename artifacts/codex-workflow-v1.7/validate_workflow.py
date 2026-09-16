"""Offline import/wiring contract check. Usage: python validate_workflow.py [file]."""
import json,pathlib,re,sys
p=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else pathlib.Path(__file__).with_name('TYrag Enterprise QA Agent v1.7 Chat Baseline.json')
w=json.loads(p.read_text(encoding='utf-8-sig'))
c=w['components'];ns={n['id']:n for n in w['graph']['nodes']};edges=w['graph']['edges']
assert 'dsl' not in w and set(ns)==set(c) and len(ns)==5 and len(edges)==4
for k,v in c.items():
 assert ns[k]['data']['form']==v['obj']['params'],f'UI/runtime mismatch: {k}'
 assert set(v['downstream'])=={e['target'] for e in edges if e['source']==k}
 assert set(v['upstream'])=={e['source'] for e in edges if e['target']==k}
 for ref in re.findall(r'\{([A-Za-z]+:[A-Za-z0-9]+|begin)@([A-Za-z0-9_]+)\}',json.dumps(v['obj']['params'])):
  assert ref[0] in c,f'dangling reference: {ref}'
  pr=c[ref[0]]['obj']['params'];assert ref[1] in pr.get('outputs',{}) or ref[1] in pr.get('inputs',{}),ref
r=c['Retrieval:FocusedEvidence']['obj']['params']
assert (r['top_n'],r['top_k'],r['similarity_threshold'],r['keywords_similarity_weight'])==(6,10,0.1,0.7)
assert r['rerank_id']=='qwen3-rerank@千问@Tongyi-Qianwen'
assert r['query']=='Agent:QueryRefiner@content'
assert r['dataset_ids']==['begin@authorized_dataset_ids']
assert r['doc_scope_ids']==['begin@authorized_doc_ids'] and r['doc_scope_mode']=='restrict'
assert not r['use_kg']
_mdf=r['meta_data_filter']
assert _mdf.get('method')=='semi_auto'
assert _mdf.get('logic','and') in ('and','or')
assert isinstance(_mdf.get('manual',[]), list)
_sa=_mdf.get('semi_auto') or []
assert any((x=='equipment_id') or (isinstance(x,dict) and x.get('key')=='equipment_id') for x in _sa)
# QueryRefiner must pin equipment_id into query for semi_auto extraction
_qr=c['Agent:QueryRefiner']['obj']['params']['sys_prompt']
assert 'equipment_id' in _qr and ('禁止编造' in _qr or '不编造' in _qr)
for x in ['authorized_doc_ids','authorized_dataset_ids']:
 assert json.loads(c['begin']['obj']['params']['inputs'][x]['value'])==[]
for x in ['Agent:FocusedAnswer','Agent:QueryRefiner']:
 a=c[x]['obj']['params'];assert not a['tools'] and not a['mcp']
 assert a['thinking']=='disabled'
a=c['Agent:FocusedAnswer']['obj']['params']
assert '{begin@user_memory}' in a['sys_prompt'] and '{Retrieval:FocusedEvidence@formalized_content}' in a['sys_prompt']
assert c['Message:FinalAnswer']['obj']['params']['content']==['{Agent:FocusedAnswer@content}']
assert all(not w[x] for x in ['history','messages','path','retrieval','variables'])
assert not re.search(r'"(?:api_key|password|secret|authorization)"\s*:',json.dumps(w),re.I)
wv=c['begin']['obj']['params']['inputs']['workflow_version']['value']
assert wv=='enterprise-qa-agent-v1.7.1', wv
print('PASS: canonical import; 5 nodes/4 edges; graph/runtime parity; references; Chat retrieval snapshot; scope; semi_auto equipment_id; memory; clean export; v1.7.1')
