"""Medium mode: retrieve claim evidence, then independently verify it."""
import asyncio

from rag.advanced_rag.harness.types import ClaimTarget, AgentResult
from rag.advanced_rag.harness.config import get_mode
from rag.advanced_rag.harness.evidence import registry_for
from rag.advanced_rag.harness.semantic_verifier import assess_claims
from rag.advanced_rag.harness.sufficiency import claim_verdict, route_sufficiency_verdict
from rag.advanced_rag.harness.tools.search import hybrid_search


async def decompose_and_search(state: dict, tools) -> dict:
    claims = [ClaimTarget(**c) if isinstance(c, dict) else c for c in state.get('claims', [])]
    # Initial planner claims are requirements; it cannot self-certify them.
    for c in claims:
        c.required, c.is_verified, c.verification = True, False, 'unknown'
    mode = get_mode(state['route'].thinking_mode if state.get('route') else 'medium')
    registry = registry_for(tools)
    verdict = claim_verdict(claims, [])
    for cycle in range(mode.max_orchestrator_cycles):
        pending = [c for c in claims if not c.is_verified]
        pending.sort(key=lambda c: c.claim_id in registry.assessments)
        pending = pending[:8]
        results = await asyncio.gather(*(hybrid_search(tools, query=c.description,
            keywords=state.get('keywords','')) for c in pending))
        for claim, result in zip(pending, results):
            ids = registry.register(result.get('chunks', []), claim.claim_id)
            claim.agent_result = AgentResult(claim_id=claim.claim_id, report='',
                is_verified=False, confidence=0.0, evidence_ids=ids)
        assessments = await assess_claims(tools, state.get('question',''), claims)
        verdict = claim_verdict(claims, assessments)
        action, more = route_sufficiency_verdict(verdict, mode.label, cycle, mode.max_orchestrator_cycles)
        if not more:
            break
    registry.publish()
    return {'kbinfos': tools.kbinfos, 'verdict': verdict.__dict__,
            'partial_answer': verdict.status != 'SUFFICIENT',
            'abstain': not any(a['status'] == 'supported' for a in registry.assessments.values())}
