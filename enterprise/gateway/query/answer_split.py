"""Split RAGFlow think markers from the user-facing assistant answer.

Structured tags only: ``<think>...</think>`` pairs, or a trailing ``</think>``
from RAGFlow ``decorate_answer``. Untagged planning text is left in ``answer``.

Also repairs damaged empty-name wrappers (``<>...</>``) and defensively strips
grounding lightbulb timeline HTML (``think-stage`` / structured safe timeline)
out of the user-visible answer into ``reasoning``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_OPEN = "<think>"
_CLOSE = "</think>"
_PAIR_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
# Damaged wrappers seen when "<think>" was truncated/corrupted to "<>".
_DAMAGED_PAIR_RE = re.compile(r"<>(.*?)</>", re.DOTALL)

_TIMELINE_HINT_RE = re.compile(
    r"think-stage|Structured safe execution timeline",
    re.IGNORECASE,
)
# Leading timeline blob: optional damaged/open think tag, intro <p><em>..., then
# one or more <details class="think-stage">...</details> blocks.
_TIMELINE_PREFIX_RE = re.compile(
    r"""
    \A\s*
    (?:<\s*think\s*>|<>)?\s*
    (?:<p>\s*<em>\s*Structured\s+safe\s+execution\s+timeline[\s\S]*?</em>\s*</p>\s*)?
    (?:<details\b[^>]*class=["']think-stage["'][^>]*>[\s\S]*?</details>\s*)+
    (?:</\s*think\s*>|</>)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class SplitOutput:
    answer: str
    reasoning: str


def _join_reasoning(*parts: str) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _repair_damaged_think_tags(text: str) -> str:
    """Map ``<>...</>`` wrappers back to ``<think>...</think>`` when present."""
    if "<>" not in text and "</>" not in text:
        return text
    repaired = _DAMAGED_PAIR_RE.sub(
        lambda m: f"{_OPEN}{m.group(1)}{_CLOSE}",
        text,
    )
    # Orphan opener/closer still used around timeline HTML.
    if "<>" in repaired or "</>" in repaired:
        if repaired.lstrip().startswith("<>"):
            lead = len(repaired) - len(repaired.lstrip())
            repaired = repaired[:lead] + _OPEN + repaired.lstrip()[2:]
        if "</>" in repaired:
            # Replace closers that are not already part of a repaired think pair.
            repaired = repaired.replace("</>", _CLOSE)
        repaired = repaired.replace("<>", _OPEN)
    return repaired


def _strip_timeline_html(answer: str) -> tuple[str, str]:
    """Move grounding timeline HTML from answer into reasoning if still present."""
    text = answer or ""
    if not text or not _TIMELINE_HINT_RE.search(text):
        return text, ""
    match = _TIMELINE_PREFIX_RE.search(text)
    if match:
        timeline = match.group(0)
        rest = text[match.end() :]
        timeline = _TAG_RE.sub("", timeline)
        timeline = timeline.replace("<>", "").replace("</>", "")
        return rest.strip(), timeline.strip()
    hint = _TIMELINE_HINT_RE.search(text)
    assert hint is not None
    start = hint.start()
    prefix = text[:start]
    open_suffix = re.search(r"(?:<\s*think\s*>|<>)\s*$", prefix, re.IGNORECASE)
    if open_suffix:
        start = open_suffix.start()
    last_details = None
    for m in re.finditer(
        r"<details\b[^>]*class=[\"']think-stage[\"'][^>]*>[\s\S]*?</details>",
        text,
        re.IGNORECASE,
    ):
        last_details = m
    if last_details and last_details.end() > start:
        end = last_details.end()
        after = text[end:]
        close = re.match(r"\s*(?:</\s*think\s*>|</>)", after, re.IGNORECASE)
        if close:
            end = end + close.end()
        timeline = _TAG_RE.sub("", text[start:end])
        timeline = timeline.replace("<>", "").replace("</>", "")
        return text[end:].strip(), timeline.strip()
    return text, ""


def _split_unprotected_output(raw: str | None) -> SplitOutput:
    text = _repair_damaged_think_tags(raw or "")
    if not text:
        return SplitOutput("", "")
    blocks = _PAIR_RE.findall(text)
    if blocks:
        reasoning = "\n".join(item.strip() for item in blocks if item.strip())
        answer = _PAIR_RE.sub("", text)
        rest = _split_unprotected_output(answer.strip())
        return SplitOutput(rest.answer, _join_reasoning(reasoning, rest.reasoning))
    close_at = text.lower().rfind(_CLOSE)
    if close_at >= 0:
        reasoning = _TAG_RE.sub("", text[:close_at]).strip()
        answer = text[close_at + len(_CLOSE) :].strip()
        answer, timeline = _strip_timeline_html(answer)
        return SplitOutput(answer.strip(), _join_reasoning(reasoning, timeline))
    answer, timeline = _strip_timeline_html(text)
    if timeline:
        return SplitOutput(answer.strip(), timeline)
    open_at = text.lower().find(_OPEN)
    if open_at >= 0:
        return SplitOutput(text[:open_at].strip(), text[open_at + len(_OPEN):])
    return SplitOutput(text, "")


def split_assistant_output(raw: str | None) -> SplitOutput:
    from .citation_select import _protected_ranges
    import uuid
    text = raw or ""
    prefix = uuid.uuid4().hex
    saved = {}
    for i, (start, end) in reversed(list(enumerate(_protected_ranges(text)))):
        token = f"{prefix}_{i}_"
        saved[token] = text[start:end]
        text = text[:start] + token + text[end:]
    result = _split_unprotected_output(text)
    answer, reasoning = result.answer, result.reasoning
    for token, content in saved.items():
        answer = answer.replace(token, content)
        reasoning = reasoning.replace(token, content)
    return SplitOutput(answer, reasoning)


def finalize_streamed_output(
    accumulated_answer: str | None,
    accumulated_reasoning: str | None = None,
    final_delta: str | None = None,
) -> SplitOutput:
    """Post-stream safety net before persist / outbound answer.delta.

    Re-splits the full buffers even when mid-stream flags/tags were missing or
    damaged (``<>``), so timeline HTML cannot remain in user-visible answer.
    """
    answer = accumulated_answer or ""
    reasoning = accumulated_reasoning or ""
    final_text = final_delta or ""

    use_final = bool(final_text) and (
        (not answer and not reasoning)
        or bool(_TIMELINE_HINT_RE.search(final_text))
        or bool(_PAIR_RE.search(final_text))
        or "<>" in final_text
        or "<think>" in final_text.lower()
    )
    if use_final:
        split = split_assistant_output(final_text)
        # Prefer final reasoning when present; otherwise keep prior stream reasoning.
        return SplitOutput(
            split.answer,
            _join_reasoning("" if split.reasoning else reasoning, split.reasoning),
        )

    if reasoning:
        combined = f"{_OPEN}{reasoning}{_CLOSE}{answer}"
    else:
        combined = answer
    return split_assistant_output(combined)


def public_reasoning(text: str | None) -> str | None:
    # Only report an observed processing stage; never copy model text.
    return "正在处理请求。" if text else None


def safe_execution_reasoning(text: str | None, format: str | None) -> str | None:
    return text if format == "safe_execution_v1" and text == "正在处理请求。" else None


class StreamThinkSplitter:
    """Incrementally route SSE deltas using flags and/or think tags."""

    def __init__(self) -> None:
        self._in_think = False
        self._carry = ""
        self._code = ""
        self._line = ""

    def feed(
        self,
        delta: str | None,
        *,
        start_to_think: bool = False,
        end_to_think: bool = False,
    ) -> list[tuple[str, str]]:
        if start_to_think:
            self._in_think = True
        text = self._carry + (delta or "")
        self._carry = ""
        pieces = self._split_text(text)
        if end_to_think:
            self._in_think = False
        return [(kind, chunk) for kind, chunk in pieces if chunk]

    def finish(self) -> list[tuple[str, str]]:
        carry, self._carry = self._carry, ""
        if not carry or carry.lower().startswith("<"):
            return []
        return [(self._kind(), carry)]

    def _kind(self) -> str:
        return "reasoning" if self._in_think else "answer"

    def _split_text(self, text: str) -> list[tuple[str, str]]:
        pieces: list[tuple[str, str]] = []
        def emit(value):
            if pieces and pieces[-1][0] == self._kind():
                pieces[-1] = (self._kind(), pieces[-1][1] + value)
            else:
                pieces.append((self._kind(), value))
            self._line = (self._line + value).rsplit("\n", 1)[-1]
        index = 0
        while index < len(text):
            char = text[index]
            if char in "`~" and not self._in_think:
                run = re.match(re.escape(char) + "+", text[index:]).group(0)
                if index + len(run) == len(text):
                    self._carry = text[index:]
                    break
                if self._code:
                    if run == self._code or (len(self._code) >= 3 and run[0] == self._code[0] and len(run) >= len(self._code)):
                        self._code = ""
                elif char == "`" or (len(run) >= 3 and not self._line.strip()):
                    self._code = run
                emit(run)
                index += len(run)
                continue
            indented = self._line.startswith(("    ", "\t"))
            escaped = (len(self._line) - len(self._line.rstrip("\\"))) % 2 == 1
            if char == "<" and not self._code and not indented and not escaped:
                tail = text[index:].lower()
                tag = next((tag for tag in (_OPEN, _CLOSE) if tail.startswith(tag)), None)
                if tag:
                    self._in_think = tag == _OPEN
                    index += len(tag)
                    continue
                if _OPEN.startswith(tail) or _CLOSE.startswith(tail):
                    self._carry = text[index:]
                    break
            emit(char)
            index += 1
        return pieces


def _incomplete_tag_suffix(text: str) -> int:
    for length in range(min(len(_CLOSE), len(text)), 0, -1):
        suffix = text[-length:].lower()
        if _OPEN.startswith(suffix) or _CLOSE.startswith(suffix):
            return length
    return 0
