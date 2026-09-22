"""Remove payloads from logging records while enterprise scoped calls execute."""
from contextvars import ContextVar
from functools import wraps
import logging

_active = ContextVar('enterprise_safe_logging', default=False)
_installed = False


def install():
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if _active.get():
            # Sanitize before any handler (file, console or think forwarding).
            record.msg = 'enterprise_execution_event'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record
    logging.setLogRecordFactory(factory)
    _installed = True


def active():
    return _active.get()


def scoped_execution(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        install()
        token = _active.set(_active.get() or kwargs.get('doc_scope_mode') == 'restrict'
                            or bool(kwargs.get('disable_langfuse'))
                            or "authorized_doc_ids" in (kwargs.get("inputs") or {}))
        try:
            async for item in fn(*args, **kwargs):
                yield item
        finally:
            _active.reset(token)
    return wrapped
