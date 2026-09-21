#!/usr/bin/env python3
"""Score independently reviewed blind runs, never fabricate missing results.

Input JSONL records contain case_id, mode (simple/medium/high/ultra),
claimed_sufficient (boolean, or null for simple), answer_correct (reviewer bool),
citation_correct/citation_total (reviewer counts), elapsed_ms, extra_model_calls.
No answer bodies are printed. Missing combinations are a hard error.
"""
import argparse
import json
from pathlib import Path
import statistics

MODES = ('simple','medium','high','ultra')


def score(cases, rows):
    expected = {(c['id'], mode) for c in cases for mode in MODES}
    records = {}
    by_id = {c['id']: c for c in cases}
    for row in rows:
        key = (row['case_id'], row['mode'])
        if key not in expected or key in records:
            raise ValueError('Duplicate or unknown evaluation record')
        if type(row['answer_correct']) is not bool or row['claimed_sufficient'] not in (True, False, None):
            raise ValueError('Missing independent review')
        if key[1] != 'simple' and type(row['claimed_sufficient']) is not bool:
            raise ValueError('Missing observed sufficiency verdict')
        for field in ('citation_correct','citation_total','elapsed_ms','extra_model_calls'):
            if type(row[field]) not in (int,float) or row[field] < 0:
                raise ValueError('Invalid measurement')
        if row['citation_correct'] > row['citation_total']:
            raise ValueError('Invalid citation review')
        records[key] = row
    if set(records) != expected:
        raise ValueError(f'Incomplete evaluation: {len(expected-set(records))} runs missing')
    result = {}
    for mode in MODES:
        group = [r for (case,m),r in records.items() if m==mode]
        total_citations = sum(r['citation_total'] for r in group)
        eligible = [r for r in group if by_id[r['case_id']]['expected_answerable'] and r['answer_correct']]
        result[mode] = dict(cases=len(group), correct_answers=sum(r['answer_correct'] for r in group),
            false_sufficient=sum(r['claimed_sufficient'] is True and
                (not r['answer_correct'] or not by_id[r['case_id']]['expected_answerable']) for r in group) if mode!='simple' else None,
            correct_supported_downgraded=sum(r['claimed_sufficient'] is False for r in eligible) if mode!='simple' else None,
            correct_supported_cases=len(eligible),
            citation_accuracy=sum(r['citation_correct'] for r in group)/total_citations if total_citations else None,
            median_elapsed_ms=statistics.median(r['elapsed_ms'] for r in group),
            extra_model_calls=sum(r['extra_model_calls'] for r in group))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviews',type=Path,required=True)
    args=parser.parse_args()
    cases=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/f06-blind-evaluation.json').read_text())
    try:
        with args.reviews.open() as source:
            result=score(cases,[json.loads(line) for line in source if line.strip()])
    except (ValueError,KeyError,TypeError,OSError):
        parser.exit(2,'Evaluation incomplete or invalid; no acceptance report generated.\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
