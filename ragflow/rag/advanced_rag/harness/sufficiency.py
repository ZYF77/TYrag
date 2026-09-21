"""Sufficiency follows verified required claims, never retrieval or confidence."""
from rag.advanced_rag.harness.types import SufficiencyVerdict


def claim_verdict(claims, assessments):
    by_id = {a['claim_id']: a for a in assessments}
    required = [c for c in claims if c.required]
    missing = [c.claim_id for c in required if by_id.get(c.claim_id, {}).get('status') != 'supported']
    supported = sum(by_id.get(c.claim_id, {}).get('status') == 'supported' for c in required)
    conflicts = any(a.get('status') == 'contradicted' for a in assessments)
    if conflicts:
        status = 'CONFLICTING'
    elif required and not missing:
        status = 'SUFFICIENT'
    elif supported:
        status = 'USEFUL_BUT_INCOMPLETE'
    elif any(c.agent_result and c.agent_result.evidence_ids for c in required):
        status = 'INSUFFICIENT'
    else:
        status = 'UNANSWERABLE'
    score = supported / max(1, len(required))
    return SufficiencyVerdict(status=status, score=score, agent_score=0.0, cross_score=score,
        claim_assessments=[dict(claim_id=c.claim_id, status=by_id.get(c.claim_id, {}).get('status', 'unknown'),
                                is_verified=by_id.get(c.claim_id, {}).get('status') == 'supported') for c in claims],
        has_conflicts=conflicts, missing_claims=missing,
        feedback='; '.join(f'{cid}: evidence incomplete' for cid in missing),
        overall_reason=f'{status}: {supported}/{len(required)} required claims supported')


def route_sufficiency_verdict(verdict, mode_label, cycle, max_cycles):
    if verdict.status == 'SUFFICIENT':
        return 'ANSWER', False
    if cycle + 1 < max_cycles:
        return 'CONTINUE', True
    if verdict.status in {'USEFUL_BUT_INCOMPLETE','CONFLICTING'}:
        return 'ANSWER_PARTIAL', False
    return 'ABSTAIN', False
