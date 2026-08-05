"""Document and sync event state machines from contracts/status-state-machine.md."""

SYNC_EVENT_STATES = frozenset({
    "received", "validating", "accepted", "transferring", "registering",
    "tracking", "completed", "retry_wait", "failed", "deduplicated",
})

DOCUMENT_STATES = frozenset({
    "received", "validated", "registered", "queued", "parsing", "indexing",
    "validating", "ready", "review_required", "superseded", "disabled",
    "deleted", "cancelled", "failed", "retry_wait",
})

TERMINAL_DOCUMENT_STATES = frozenset({
    "ready", "superseded", "disabled", "deleted", "failed", "cancelled",
})

_EVENT_TRANSITIONS: dict[str, set[str]] = {
    "received": {"validating", "accepted", "failed", "deduplicated", "retry_wait"},
    "validating": {"accepted", "transferring", "failed", "retry_wait", "deduplicated"},
    "accepted": {"transferring", "registering", "failed", "retry_wait"},
    "transferring": {"registering", "tracking", "failed", "retry_wait"},
    "registering": {"tracking", "completed", "failed", "retry_wait"},
    "tracking": {"completed", "failed", "retry_wait"},
    "completed": {"failed", "retry_wait", "deduplicated"},
    "retry_wait": {
        "received", "validating", "accepted", "transferring", "registering",
        "tracking", "failed",
    },
    "failed": {"retry_wait", "accepted"},
    "deduplicated": set(),
}

_DOCUMENT_TRANSITIONS: dict[str, set[str]] = {
    "received": {"validated", "registered", "failed", "retry_wait"},
    "validated": {"accepted", "registered", "failed", "retry_wait"},
    "accepted": {"transferring", "registered", "failed", "retry_wait"},
    "transferring": {"registering", "tracking", "failed", "retry_wait"},
    "registering": {"tracking", "queued", "failed", "retry_wait"},
    "tracking": {"registered", "queued", "parsing", "ready", "failed",
                 "review_required", "retry_wait", "cancelled"},
    "registered": {"queued", "parsing", "tracking", "ready", "failed",
                   "review_required", "retry_wait", "cancelled"},
    "queued": {"parsing", "indexing", "ready", "failed", "review_required",
               "retry_wait", "cancelled"},
    "parsing": {"indexing", "validating", "ready", "failed", "review_required",
                "retry_wait", "cancelled"},
    "indexing": {"validating", "ready", "failed", "review_required", "retry_wait"},
    "validating": {"ready", "failed", "review_required", "retry_wait"},
    "review_required": {"ready", "failed", "registered", "queued", "parsing",
                        "disabled", "deleted", "retry_wait"},
    "ready": {"superseded", "disabled", "deleted", "review_required"},
    "superseded": {"disabled", "deleted"},
    "disabled": {"registered", "ready", "deleted"},
    "deleted": {"registered"},
    "cancelled": {"registered", "failed"},
    "retry_wait": {
        "received", "validated", "accepted", "transferring", "registering",
        "tracking", "registered", "queued", "parsing", "indexing", "validating",
        "failed",
    },
    "failed": {"retry_wait", "accepted", "registered", "queued"},
}


def transition_allowed(current: str, next_status: str, kind: str = "document") -> bool:
    """Return True when next_status is a legal transition from current."""
    if current == next_status:
        return True
    table = _DOCUMENT_TRANSITIONS if kind == "document" else _EVENT_TRANSITIONS
    return next_status in table.get(current, set())


def is_terminal_document_status(status: str) -> bool:
    return status in TERMINAL_DOCUMENT_STATES


def validate_transition(current: str, next_status: str, kind: str = "document") -> None:
    if not transition_allowed(current, next_status, kind):
        raise ValueError(
            f"Invalid {kind} state transition: {current} -> {next_status}"
        )
