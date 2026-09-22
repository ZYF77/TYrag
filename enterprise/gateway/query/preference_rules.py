"""Finite, confirmed presentation preferences; never domain knowledge or instructions."""
import re

VALUES = {
    "language": {"zh": "使用简体中文", "en": "使用英语"},
    "detail": {"brief": "简短回答", "detailed": "详细回答"},
    "format": {"paragraphs": "使用段落", "list": "使用列表"},
}
_PATTERNS = {
    "language": {"zh": r"用(?:简体)?中文(?:回答)?", "en": r"用英语(?:回答)?"},
    "detail": {"brief": r"(?:简短|简洁)(?:地)?回答", "detailed": r"详细(?:地)?回答"},
    "format": {"paragraphs": r"用段落(?:回答)?", "list": r"用列表(?:回答)?"},
}


def extract_preferences(question: str) -> dict[str, str]:
    # Full utterance grammar deliberately excludes quotes, negation, technical
    # facts and one-off instructions. Ambiguous sentences produce no candidate.
    text = question.strip().rstrip("。！!")
    for key, patterns in _PATTERNS.items():
        for value, pattern in patterns.items():
            if re.fullmatch(r"(?:以后|今后)(?:都)?(?:请)?" + pattern, text):
                return {key: value}
    return {}


def preference_text(values: dict[str, str]) -> str:
    lines = [VALUES[k][v] for k, v in sorted(values.items()) if k in VALUES and v in VALUES[k]]
    return "已确认的表达偏好（当前问题的明确要求优先）：" + "；".join(lines) if lines else ""
