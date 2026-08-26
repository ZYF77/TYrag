"""Classify LiteLLM / Dashscope error payloads into Gateway error codes."""
from __future__ import annotations

_ERROR_ANCHORS = (
    "error",
    "litellm",
    "dashscope",
    "arrearage",
    "invalid_parameter",
    "badrequesterror",
    "invalid_request",
)


def classify_llm_provider_error(text: str) -> tuple[str, int] | None:
    """Map provider error text to ``(code, http_status)``, or ``None`` if not a hit.

    Priority: modality → billing/quota → other provider rejection.
    Only classifies when the text looks like an error payload (anchors),
    so normal Chinese answers are left alone.
    """
    if not text or not isinstance(text, str):
        return None

    lowered = text.lower()
    has_anchor = any(anchor in lowered for anchor in _ERROR_ANCHORS) or ("欠费" in text)
    if not has_anchor:
        return None

    # 1. Multimodal / image rejected by text-only model
    if "unexpected item type in content" in lowered:
        return "LLM_MODALITY_UNSUPPORTED", 502
    if "image_url" in lowered and "invalid_parameter" in lowered:
        return "LLM_MODALITY_UNSUPPORTED", 502
    if "multimodal" in lowered and (
        "not support" in lowered or "unsupported" in lowered or "does not support" in lowered
    ):
        return "LLM_MODALITY_UNSUPPORTED", 502

    # 2. Billing / quota / arrearage
    if "arrearage" in lowered or "欠费" in text:
        return "LLM_PROVIDER_BILLING", 502
    if "insufficient" in lowered and "quota" in lowered:
        return "LLM_PROVIDER_BILLING", 502
    if "access denied" in lowered and (
        "quota" in lowered or "balance" in lowered or "billing" in lowered
    ):
        return "LLM_PROVIDER_BILLING", 502
    if "billing" in lowered and (
        "error" in lowered or "denied" in lowered or "deny" in lowered or "fail" in lowered
    ):
        return "LLM_PROVIDER_BILLING", 502

    # 3. Other provider / LiteLLM rejections
    if (
        "invalid_request" in lowered
        or "litellm.badrequesterror" in lowered
        or "dashscopeexception" in lowered
    ):
        return "LLM_PROVIDER_REJECTED", 502

    return None
