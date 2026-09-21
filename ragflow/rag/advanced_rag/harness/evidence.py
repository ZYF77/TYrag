"""Request-local, append-only evidence identities and immutable source snapshots."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit


class EvidenceRegistry:
    def __init__(self, tools):
        self.tools = tools
        self.tenants = frozenset(str(t) for t in tools.tenant_ids)
        self.datasets = frozenset(str(k) for k in getattr(tools, 'metadata_kb_ids', None) or tools.kb_ids)
        self.dataset_tenants = {str(k.id): str(k.tenant_id) for k in list(getattr(tools, 'kbs', [])) + list(getattr(tools, 'sql_kbs', []))}
        scope = getattr(tools, 'doc_scope', None)
        self.documents = None if scope is None else frozenset(scope)
        if getattr(tools, 'doc_scope_mode', None) == 'restrict' and scope is None:
            self.documents = frozenset()
        self.web_enabled = bool(getattr(tools, 'web_search', None)) and self.documents != frozenset()
        self._keys = {}
        self._snapshots = {}
        self.claim_ids: dict[str, set[int]] = {}
        self.assessments: dict[str, dict] = {}
        self.calls = 0
        self.cache = {}
        self._semaphore = None

    def source_key(self, chunk):
        if not isinstance(chunk, dict):
            return None
        content = chunk.get('content_with_weight') or chunk.get('text')
        if not isinstance(content, str) or not content.strip():
            return None
        url = chunk.get('url')
        # Web providers use an empty kb_id. A document carrying a URL remains
        # document evidence and must pass the same dataset/document ceiling.
        if url and not chunk.get('kb_id'):
            try:
                parsed = urlsplit(url)
                if not self.web_enabled or parsed.scheme not in ('https', 'http') or not parsed.hostname:
                    return None
            except (TypeError, ValueError):
                return None
            source = ['web', url]
        else:
            kb = chunk.get('kb_id')
            doc = chunk.get('document_id') or chunk.get('doc_id')
            cid = chunk.get('chunk_id') or chunk.get('id')
            if chunk.get('document_id') and chunk.get('doc_id') and chunk['document_id'] != chunk['doc_id']:
                return None
            if not all(isinstance(v, str) and v for v in (kb, doc, cid)) or kb not in self.datasets:
                return None
            tenant = chunk.get('tenant_id') or self.dataset_tenants.get(kb)
            if not tenant and len(self.tenants) == 1:
                tenant = next(iter(self.tenants))
            if tenant not in self.tenants or (kb in self.dataset_tenants and self.dataset_tenants[kb] != tenant):
                return None
            if self.documents is not None and doc not in self.documents:
                return None
            source = ['document', tenant, kb, doc, cid, chunk.get('source_version_id') or chunk.get('version') or '']
        return hashlib.sha256(json.dumps([source, content], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def register(self, chunks, claim_id=None):
        ids = []
        for chunk in chunks:
            key = self.source_key(chunk)
            if key is None:
                continue
            if key not in self._keys:
                eid = len(self._keys)
                self._keys[key] = eid
                snapshot = deepcopy(chunk)
                snapshot['_evidence_id'] = eid
                self._snapshots[eid] = snapshot
            eid = self._keys[key]
            if eid not in ids:
                ids.append(eid)
        if claim_id is not None:
            self.claim_ids.setdefault(claim_id, set()).update(ids)
        self.publish()
        return ids

    def get(self, eid):
        return deepcopy(self._snapshots.get(eid))

    def for_claim(self, claim_id, ids):
        allowed = self.claim_ids.get(claim_id, set())
        ids = ids if isinstance(ids, list) else []
        return {eid: self.get(eid) for eid in ids if type(eid) is int and eid in allowed and eid in self._snapshots}

    def publish(self):
        from rag.utils.parent_chunks import parent_doc_aggs
        chunks = [self.get(i) for i in self._snapshots]
        self.tools.kbinfos['chunks'] = chunks
        self.tools.kbinfos['doc_aggs'] = parent_doc_aggs(chunks)


def registry_for(tools):
    registry = getattr(tools, '_evidence_registry', None)
    if registry is None:
        registry = EvidenceRegistry(tools)
        tools._evidence_registry = registry
        registry.register(list(tools.kbinfos.get('chunks', [])))
    return registry
