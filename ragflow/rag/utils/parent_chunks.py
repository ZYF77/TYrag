"""RF-PATCH: parent chunks carry source and parse-task identities, never text alone."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections import defaultdict

_ID = re.compile(r"^p2\.([0-9a-f]{32})\.([0-9a-f]{64})$")
_FIELDS = ["content_with_weight", "doc_id", "docnm_kwd", "kb_id", "available_int",
           "position_int", "create_timestamp_flt", "page_num_int", "top_int", "doc_type_kwd"]


def _digest(tenant_id, kb_id, doc_id, generation, content):
    material = json.dumps(["parent-v2", tenant_id, kb_id, doc_id, generation, content],
                          ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def parent_id(tenant_id: str, kb_id: str, doc_id: str, task_id: str, content: str) -> str | None:
    if not all(isinstance(v, str) and v.strip() for v in (tenant_id, kb_id, doc_id, task_id, content)):
        return None
    generation = hashlib.sha256(task_id.encode()).hexdigest()[:32]
    return f"p2.{generation}.{_digest(tenant_id, kb_id, doc_id, generation, content)}"


def create_parent_chunks(chunks: list[dict], tenant_id: str, kb_id: str, task_id: str) -> list[dict]:
    parents = {}
    for child in chunks:
        content = child.get("mom") or child.get("mom_with_weight") or ""
        # Reprocessing must never retain a previous/legacy pointer after failure.
        child.pop("mom_id", None)
        if child.get("kb_id") != kb_id:
            continue
        pid = parent_id(tenant_id, kb_id, child.get("doc_id"), task_id, content)
        if not pid:
            continue
        child["mom_id"] = pid
        if pid not in parents:
            parent = {key: copy.deepcopy(child[key]) for key in _FIELDS if key in child}
            parent.update(id=pid, content_with_weight=content, available_int=0)
            parents[pid] = parent
    return list(parents.values())


def _valid_parent(pid, parent, tenant_id, kb_id, doc_id):
    match = _ID.fullmatch(pid)
    content = parent.get("content_with_weight")
    return bool(match and isinstance(content, str) and parent.get("doc_id") == doc_id
                and parent.get("kb_id") == kb_id
                and _digest(tenant_id, kb_id, doc_id, match[1], content) == match[2])


def expand_parents(store, chunks: list[dict], tenant_ids: list[str], index_name, order_factory) -> list[dict]:
    """Restrict *before* fetching; missing, old or ambiguous parents keep children."""
    retained = []
    groups = defaultdict(list)
    for child in chunks:
        pid = child.get("mom_id")
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or not isinstance(child.get("kb_id"), str) or not child["kb_id"] or not isinstance(child.get("doc_id"), str) or not child["doc_id"]:
            retained.append(child)
        else:
            groups[(child["kb_id"], child["doc_id"], pid)].append(child)
    for (kb_id, doc_id, pid), children in groups.items():
        matches = []
        failed = False
        for tenant_id in dict.fromkeys(tenant_ids or []):
            try:
                result = store.search(_FIELDS, [], {"id": [pid], "kb_id": [kb_id], "doc_id": [doc_id]},
                    [], order_factory(), 0, 2, index_name(tenant_id), [kb_id])
                fields = store.get_fields(result, _FIELDS)
                # More than one record is ambiguous even when one looks valid.
                if len(fields) > 1:
                    failed = True
                for fetched_id, parent in fields.items():
                    if fetched_id != pid or not _valid_parent(pid, parent, tenant_id, kb_id, doc_id):
                        failed = True
                    else:
                        matches.append(parent)
            except Exception:
                # No body/exception payload in diagnostics; retain already scoped evidence.
                logging.warning("Parent expansion failed; retaining scoped child chunks")
                failed = True
        if failed or len(matches) != 1:
            retained.extend(children)
            continue
        parent = matches[0]
        expanded = {
            "chunk_id": pid, "content_with_weight": parent["content_with_weight"],
            "doc_id": doc_id, "kb_id": kb_id, "docnm_kwd": parent.get("docnm_kwd", ""),
            "content_ltks": " ".join(c.get("content_ltks", "") for c in children),
            "positions": parent.get("position_int", []),
            "doc_type_kwd": parent.get("doc_type_kwd", ""),
            "image_id": "", "important_kwd": list(dict.fromkeys(k for c in children for k in c.get("important_kwd", []))),
            "vector": children[0].get("vector", []),
        }
        for field in ("similarity", "vector_similarity", "term_similarity"):
            expanded[field] = sum(float(c.get(field, c.get("similarity", 0))) for c in children) / len(children)
        retained.append(expanded)
    return sorted(retained, key=lambda c: float(c.get("similarity", 0)), reverse=True)


def parent_doc_aggs(chunks):
    result = {}
    for chunk in chunks:
        key = (chunk.get("kb_id"), chunk.get("doc_id"))
        if not key[1]:
            continue
        agg = result.setdefault(key, {"doc_id": key[1], "doc_name": chunk.get("docnm_kwd", ""), "count": 0})
        agg["count"] += 1
    return sorted(result.values(), key=lambda a: a["count"], reverse=True)
