from types import SimpleNamespace

from api.db.services import dialog_service
from api.db.services.conversation_service import structure_answer


def test_grounding_prompt_summary_is_non_empty_and_safe():
    event = dialog_service._grounding_abstain_event()

    assert event["prompt"] == dialog_service._GROUNDING_PROMPT_SUMMARY
    assert "question" not in event["prompt"].lower()


def test_structure_answer_persists_safe_prompt_on_final_message():
    conv = SimpleNamespace(
        message=[{"role": "user", "content": "question"}],
        reference=[{"chunks": [], "doc_aggs": []}],
    )
    prompt = dialog_service._GROUNDING_PROMPT_SUMMARY

    structure_answer(
        conv,
        {
            "answer": "<think>[Planner] planned the retrieval steps</think>answer",
            "reference": {},
            "prompt": prompt,
            "final": True,
        },
        "message-1",
        "session-1",
    )

    assert conv.message[-1]["prompt"] == prompt
    assert conv.message[-1]["content"].startswith("<think>")


def test_structure_answer_does_not_persist_raw_prompt():
    conv = SimpleNamespace(
        message=[{"role": "user", "content": "question"}],
        reference=[{"chunks": [], "doc_aggs": []}],
    )

    structure_answer(
        conv,
        {"answer": "answer", "reference": {}, "prompt": "SECRET SYSTEM PROMPT", "final": True},
        "message-1",
        "session-1",
    )

    assert "prompt" not in conv.message[-1]


def test_append_public_think_trace_keeps_supplied_safe_stages():
    answer = dialog_service._append_public_think_trace(
        "answer",
        ["[Planner] planned the retrieval steps", "[Planner] planned the retrieval steps"],
    )

    assert answer == "<think>[Planner] planned the retrieval steps\n[Planner] planned the retrieval steps</think>answer"
    assert "SECRET" not in answer
