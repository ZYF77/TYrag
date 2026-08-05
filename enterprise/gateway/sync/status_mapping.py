"""RAGFlow run status to enterprise sync_status mapping.

Based on RAGFlow v0.26.4 public API:
  api/apps/services/document_api_service.py  _process_run_mapping()
  common/constants.py  TaskStatus enum

  "0" / UNSTART  →  "registered"
  "1" / RUNNING  →  "parsing"
  "2" / CANCEL   →  "cancelled"
  "3" / DONE     →  "ready"
  "4" / FAIL     →  "failed"
"""

_RAGFLOW_TO_ENTERPRISE: dict[str, str] = {
    "UNSTART": "registered",
    "RUNNING": "parsing",
    "CANCEL": "cancelled",
    "DONE": "ready",
    "FAIL": "failed",
    # numeric aliases (RAGFlow may return raw values)
    "0": "registered",
    "1": "parsing",
    "2": "cancelled",
    "3": "ready",
    "4": "failed",
}

_ENTERPRISE_STAGE: dict[str, str] = {
    "received": "received",
    "validated": "validated",
    "registered": "registered",
    "parsing": "parsing",
    "ready": "ready",
    "failed": "failed",
    "cancelled": "cancelled",
    "review_required": "review_required",
}


def map_ragflow_run_to_sync_status(ragflow_run: str | None) -> str:
    """Map a RAGFlow run field to an enterprise sync_status.

    If the run value is unknown or missing, returns "registered"
    as the safest non-terminal state (document exists but status unconfirmed).
    """
    if ragflow_run is None:
        return "registered"
    return _RAGFLOW_TO_ENTERPRISE.get(str(ragflow_run), "registered")


def enterprise_stage(sync_status: str) -> str | None:
    """Return the human-readable stage for a sync_status value."""
    return _ENTERPRISE_STAGE.get(sync_status)
