"""Split RAGFlow think markers from the user-facing assistant answer.

Structured tags only: ``<think>...</think>`` pairs, or a trailing ``</think>``
from RAGFlow ``decorate_answer``. Untagged planning text is left in ``answer``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_OPEN = "<think>"
_CLOSE = "</think>"
_PAIR_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


@dataclass(frozen=True)
class SplitOutput:
    answer: str
    reasoning: str


def split_assistant_output(raw: str | None) -> SplitOutput:
    text = raw or ""
    if not text:
        return SplitOutput("", "")
    blocks = _PAIR_RE.findall(text)
    if blocks:
        reasoning = "\n".join(item.strip() for item in blocks if item.strip())
        answer = _PAIR_RE.sub("", text)
        return SplitOutput(answer.strip(), reasoning)
    close_at = text.lower().rfind(_CLOSE)
    if close_at >= 0:
        reasoning = _TAG_RE.sub("", text[:close_at]).strip()
        answer = text[close_at + len(_CLOSE) :].strip()
        return SplitOutput(answer, reasoning)
    return SplitOutput(text, "")


def public_reasoning(text: str | None) -> str | None:
    value = (text or "").strip()
    return value or None


class StreamThinkSplitter:
    """Incrementally route SSE deltas using flags and/or think tags."""

    def __init__(self) -> None:
        self._in_think = False
        self._carry = ""

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

    def _kind(self) -> str:
        return "reasoning" if self._in_think else "answer"

    def _split_text(self, text: str) -> list[tuple[str, str]]:
        pieces: list[tuple[str, str]] = []
        index = 0
        while index < len(text):
            open_at = text.find(_OPEN, index)
            close_at = text.find(_CLOSE, index)
            if open_at < 0 and close_at < 0:
                carry_len = _incomplete_tag_suffix(text[index:])
                if carry_len:
                    chunk = text[index : len(text) - carry_len]
                    if chunk:
                        pieces.append((self._kind(), chunk))
                    self._carry = text[len(text) - carry_len :]
                elif text[index:]:
                    pieces.append((self._kind(), text[index:]))
                break
            if open_at >= 0 and (close_at < 0 or open_at < close_at):
                tag_at, tag_len, opening = open_at, len(_OPEN), True
            else:
                tag_at, tag_len, opening = close_at, len(_CLOSE), False
            if tag_at > index:
                pieces.append((self._kind(), text[index:tag_at]))
            self._in_think = opening
            index = tag_at + tag_len
        return pieces


def _incomplete_tag_suffix(text: str) -> int:
    for length in range(min(len(_CLOSE), len(text)), 0, -1):
        suffix = text[-length:]
        if _OPEN.startswith(suffix) or _CLOSE.startswith(suffix):
            return length
    return 0
