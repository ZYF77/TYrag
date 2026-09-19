"""EAM Memory subject helpers.

subject = eam:{tenant_id}:{business_user_id} derived only from UserPrincipal.
Never accept client-reported userId for attribution.
"""
from __future__ import annotations

import re
from typing import Any

SUBJECT_PREFIX = "eam"
_SUBJECT_RE = re.compile(r"^eam:[^:]+:.+$")


def memory_subject_from_principal(principal: Any) -> str:
    """Build the canonical Memory user_id/subject from an authenticated principal."""
    tenant_id = str(getattr(principal, "tenant_id", "") or "").strip()
    business_user_id = str(getattr(principal, "business_user_id", "") or "").strip()
    if not tenant_id or not business_user_id:
        raise ValueError("tenant_id and business_user_id are required for memory subject")
    if ":" in tenant_id:
        raise ValueError("tenant_id must not contain ':'")
    return f"{SUBJECT_PREFIX}:{tenant_id}:{business_user_id}"


def is_valid_memory_subject(subject: str | None) -> bool:
    value = str(subject or "").strip()
    return bool(value) and _SUBJECT_RE.match(value) is not None


def parse_memory_subject(subject: str) -> tuple[str, str]:
    """Return (tenant_id, business_user_id) or raise ValueError."""
    value = str(subject or "").strip()
    if not is_valid_memory_subject(value):
        raise ValueError(f"invalid memory subject: {subject!r}")
    _, tenant_id, business_user_id = value.split(":", 2)
    return tenant_id, business_user_id