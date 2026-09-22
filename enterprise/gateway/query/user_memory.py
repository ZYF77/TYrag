"""Confirmed presentation preferences; old QA memories are never injected."""
from __future__ import annotations

import logging
from typing import Any

from enterprise.gateway.config import config
from enterprise.gateway.query.memory_client import RAGFlowMemoryClient

logger = logging.getLogger(__name__)

_memory_client: RAGFlowMemoryClient | None = None


def _user_memory_runtime():
    """Effective hot-reloadable snapshot (not frozen boot-only config)."""
    return config.runtime_settings()


def user_memory_enabled() -> bool:
    """Feature gate from runtime snapshot (env/Console; default false)."""
    return bool(_user_memory_runtime().user_memory_enabled)


def enterprise_memory_id() -> str:
    return str(_user_memory_runtime().user_memory_id or "").strip()


def user_memory_timeout_seconds() -> float:
    return float(_user_memory_runtime().user_memory_timeout_seconds)


def memory_config_ready() -> bool:
    return user_memory_enabled() and bool(enterprise_memory_id())


def get_memory_client() -> RAGFlowMemoryClient:
    global _memory_client
    if _memory_client is None:
        _memory_client = RAGFlowMemoryClient()
    return _memory_client


def set_memory_client_for_tests(client: RAGFlowMemoryClient | None) -> None:
    global _memory_client
    _memory_client = client



async def fetch_user_memory_text(
    principal: Any,
    query: str,
    *,
    request_id: str | None = None,
) -> str:
    """Inject only canonical confirmed enum values; remote and old QA are untrusted."""
    if not user_memory_enabled():
        return ""
    try:
        from enterprise.gateway.app import get_gateway_db
        from .preference_store import list_state
        from .preference_rules import preference_text
        db = await get_gateway_db()
        async with db.transaction(write=False) as conn:
            state = await list_state(conn, tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id)
        return preference_text({item["key"]: item["value"] for item in state["preferences"]})
    except Exception:
        logger.warning("preference_read_unavailable")
        return ""


def schedule_memory_candidate(*args, **kwargs) -> None:
    """Old callers cannot export QA. Candidates are captured in terminal transactions."""
    return None
