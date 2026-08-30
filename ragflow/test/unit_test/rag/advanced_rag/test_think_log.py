from logging import LogRecord, makeLogRecord

from rag.advanced_rag.think_log import (
    ThinkLogHandler,
    public_think_log,
    public_think_log_detail,
    reset_think_log_sink,
    set_think_log_sink,
)


def test_public_think_log_keeps_stage_tag_only():
    assert public_think_log('[Hybrid search] Searching the knowledge base for "西门子运行信息"') == "[Hybrid search]"
    assert public_think_log('[Planner] Working out how to research this factual question: "压力是多少"') == "[Planner]"
    assert public_think_log("no tag here") is None
    assert public_think_log("") is None


def test_public_think_log_detail_is_static_and_redacted():
    detail = public_think_log_detail('[Hybrid search] query "SECRET BODY"')

    assert detail == "[Hybrid search] ran vector and keyword retrieval"
    assert "SECRET BODY" not in detail


def test_think_log_handler_redacts_when_flag_set():
    forwarded: list[str] = []
    token = set_think_log_sink(forwarded.append, redact_content=True)
    try:
        handler = ThinkLogHandler()
        record = makeLogRecord(
            {
                "name": "rag.advanced_rag.harness.tools.search",
                "levelno": 20,
                "pathname": "search.py",
                "lineno": 1,
                "msg": '[Hybrid search] query "SECRET BODY"',
                "args": None,
                "exc_info": None,
            }
        )
        assert isinstance(record, LogRecord)
        handler.emit(record)
        assert forwarded == ["<br>[Hybrid search] ran vector and keyword retrieval"]
    finally:
        reset_think_log_sink(token)
