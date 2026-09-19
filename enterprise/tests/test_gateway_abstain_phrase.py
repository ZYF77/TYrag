# -*- coding: utf-8 -*-
"""Gateway outward no_reliable abstain must match Chat v12 phrase."""
from enterprise.gateway.app import SAFE_ERROR_MESSAGES
from enterprise.gateway.query.citation_select import ABSTAIN_PHRASE
from enterprise.gateway.query import formal_router, router
from enterprise.gateway.query.ragflow_client import _STANDARD_ABSTAIN_ANSWER


def test_gateway_outward_abstain_matches_chat_v12_phrase():
    assert ABSTAIN_PHRASE == "当前检索结果中没有找到可靠依据"
    assert formal_router.NO_RELIABLE_EVIDENCE_ANSWER == ABSTAIN_PHRASE
    assert router.NO_RELIABLE_EVIDENCE_ANSWER == ABSTAIN_PHRASE
    assert SAFE_ERROR_MESSAGES["NO_RELIABLE_EVIDENCE"] == ABSTAIN_PHRASE
    # Chat detection may still recognize the legacy empty_response wording.
    assert _STANDARD_ABSTAIN_ANSWER == "未找到可靠依据，无法回答。"
    assert "无法回答" not in formal_router.NO_RELIABLE_EVIDENCE_ANSWER
