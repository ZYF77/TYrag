#!/usr/bin/env python3
"""Read-only inspection of explicitly selected JSONL index-export records.

No services, credentials, mutations or document bodies in output. An operator
must separately authorize/export the selected documents; this is not a crawler.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ragflow'))
from rag.utils.parent_chunks import _valid_parent, _ID


def inspect_records(records, tenant_id, dataset_id, document_ids):
    selected = []
    for row in records:
        source = dict(row.get('_source', row))
        source['id'] = row.get('_id', source.get('id'))
        if source.get('kb_id') == dataset_id and source.get('doc_id') in document_ids:
            selected.append(source)
    parents = {r['id']: r for r in selected if r.get('available_int') == 0}
    counts = dict(legacy_child_count=0, new_child_count=0, verified_parent_links=0,
                  missing_or_invalid_parent_links=0)
    for row in selected:
        pid = row.get('mom_id')
        if not pid:
            continue
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            counts['legacy_child_count'] += 1
            continue
        counts['new_child_count'] += 1
        parent = parents.get(pid)
        key = 'verified_parent_links' if parent and _valid_parent(pid, parent, tenant_id, dataset_id, row['doc_id']) else 'missing_or_invalid_parent_links'
        counts[key] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--tenant-id', required=True)
    parser.add_argument('--dataset-id', required=True)
    parser.add_argument('--document-id', action='append', required=True)
    args = parser.parse_args()
    try:
        with args.input.open() as source:
            result = inspect_records((json.loads(line) for line in source if line.strip()),
                args.tenant_id, args.dataset_id, set(args.document_id))
    except (ValueError, TypeError, KeyError, OSError):
        parser.exit(2, 'Invalid or unreadable inventory input; no records changed.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
