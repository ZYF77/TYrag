"""Keep only chunks the model actually cited in the answer."""

from __future__ import annotations

import re

# Same marker grammar as RAGFlow dialog_service.CITATION_MARKER_PATTERN, with
# the legacy ``[n]`` form retained for existing Chat/Workflow answers.  The
# two capture groups distinguish explicit ID markers from legacy markers whose
# neighbouring characters need a technical-text boundary check.
_DIGITS = r"0-9\u0660-\u0669\u06F0-\u06F9"
CITATION_MARKER_PATTERN = re.compile(
    rf"\[(?:(?:ID\s*[:：]\s*)([{_DIGITS}]+)|([{_DIGITS}]+))\]",
    re.IGNORECASE,
)
# Model often writes prose "知识库ID:2、ID:5" / "以ID:5的文档为例" instead of [ID:n].
_PROSE_ID_PATTERN = re.compile(
    r"(?:知识库)?ID[:：]\s*([0-9\u0660-\u0669\u06F0-\u06F9]+)",
    re.IGNORECASE,
)
_CONTENT_FRAG_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{6,}")
ABSTAIN_PHRASE = "当前检索结果中没有找到可靠依据"
# Model often paraphrases the agreed phrase; still treat as abstain so contrast
# citations cannot surface for "暂无某某" answers.
_ABSTAIN_SIGNAL_RE = re.compile(
    "|".join(
        (
            re.escape(ABSTAIN_PHRASE),
            r"未找到可靠依据",
            r"没有找到可靠依据",
            r"暂无专门的",
            r"暂无.{0,24}(?:维修|保养|故障|工单|记录)",
            r"无法提供.{0,24}相关信息",
        )
    )
)

# Recoverable mangled citation forms → canonical [ID:n].
_MARKER_REPAIR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[+\s*ID\s*:\s*(\d+)\s*\]+", re.IGNORECASE), r"[ID:\1]"),
    (re.compile(r"\[ID\[(\d+)\]\]", re.IGNORECASE), r"[ID:\1]"),
    (re.compile(r"\[+\s*I\s*\[\s*D\s*\]\s*:\s*(\d+)\s*\]+", re.IGNORECASE), r"[ID:\1]"),
    # Model often emits [[I:D]:3] as [I:D] + :3] rather than I:D:3 inside one pair.
    (re.compile(r"\[+\s*I\s*:\s*D\s*\]\s*:\s*(\d+)\s*\]+", re.IGNORECASE), r"[ID:\1]"),
    (re.compile(r"\[+\s*I\s*:\s*D\s*:\s*(\d+)\s*\]+", re.IGNORECASE), r"[ID:\1]"),
    (re.compile(r"\[\[\s*D\s*\]\s*:\s*(\d+)\s*\]+", re.IGNORECASE), r"[ID:\1]"),
)
# Source slices are kept intact: no Markdown reserialization or whitespace cleanup.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_DEFINITION = re.compile(r" {0,3}\[([^\]\n]+)\]:")


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return start


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Conservative local scanner; mirror citationText.ts and shared fixtures."""
    blocks: list[tuple[int, int]] = []
    labels: set[str] = set()
    fence = None
    fence_start = offset = 0
    for line in re.findall(r"[^\n]*\n|[^\n]+$", text):
        # Container prefixes do not make fenced code safe to rewrite.
        logical = re.sub(r"^(?: {0,3}>[ \t]?)+", "", line)
        logical = re.sub(r"^ {0,3}(?:[-+*]|[0-9]+[.)])[ \t]+", "", logical)
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t\r\n]*", logical):
                blocks.append((fence_start, offset + len(line)))
                fence = None
        elif match := _FENCE.match(logical):
            fence = match.group(1)
            fence_start = offset
        elif logical.startswith(("    ", "\t")):
            blocks.append((offset, offset + len(line)))
        elif match := _DEFINITION.match(logical):
            labels.add(" ".join(match.group(1).lower().split()))
            blocks.append((offset, offset + len(line)))
        offset += len(line)
    if fence is not None:
        blocks.append((fence_start, len(text)))
    ranges = []
    block = i = 0
    while i < len(text):
        if block < len(blocks) and i >= blocks[block][0]:
            start, end = blocks[block]
            ranges.append((start, end))
            i = end
            block += 1
            continue
        limit = blocks[block][0] if block < len(blocks) else len(text)
        start = i
        if text[i] == "\\":
            if text[i:i + 2] == "\\[":
                end = re.search(r"\\?\]", text[i + 2:limit])
                i = i + 2 + end.end() if end else min(i + 2, limit)
            else:
                i = min(i + 2, limit)
            ranges.append((start, i))
            continue
        if text[i] == "`":
            run = re.match(r"`+", text[i:]).group(0)
            end = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", text[i + len(run):limit])
            if end:
                i += len(run) + end.end()
                ranges.append((start, i))
                continue
            i += len(run)
            continue
        if text[i] == "<":
            link = re.match(r"<(?:[A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*|[^<>\s]+@[^<>\s]+)>", text[i:limit])
            if link:
                i += len(link.group(0))
                ranges.append((start, i))
                continue
        if text[i] == "[" or text[i:i + 2] == "![":
            bracket = i + (text[i] == "!")
            end = _balanced_end(text, bracket, "[", "]")
            if end > bracket and end <= limit:
                label = " ".join(text[bracket + 1:end - 1].lower().split())
                finish = end if label in labels else start
                if text[end:end + 1] == "(":
                    finish = _balanced_end(text, end, "(", ")")
                    if finish == end:
                        finish = start
                elif text[end:end + 1] == "[":
                    ref_end = _balanced_end(text, end, "[", "]")
                    if ref_end > end:
                        ref = " ".join(text[end + 1:ref_end - 1].lower().split()) or label
                        if ref in labels:
                            finish = ref_end
                if finish > start and finish <= limit:
                    ranges.append((start, finish))
                    i = finish
                    continue
        i += 1
    return ranges


def _prose_segments(text: str):
    offset = 0
    for start, end in _protected_ranges(text):
        if start > offset:
            yield False, text[offset:start]
        yield True, text[start:end]
        offset = end
    if offset < len(text):
        yield False, text[offset:]


def _marker_allowed(text: str, match: re.Match[str]) -> bool:
    if match.group(1) is not None:
        return True
    previous = text[match.start() - 1:match.start()] if match.start() else ""
    following = text[match.end():match.end() + 1]
    return not (
        previous in (":", "]", "[") or following in (":", "]")
        or re.fullmatch(r"[A-Za-z0-9_]", previous)
        or re.fullmatch(r"[A-Za-z0-9_]", following)
    )


def _sanitize_prose(text: str) -> str:
    # Nested known damage can expose another repair (e.g. [[ID[3]]]).
    # Reach a fixed point before returning; repairs only remove syntax and
    # each legacy marker can become canonical at most once.
    while True:
        previous = text
        for pattern, replacement in _MARKER_REPAIR_PATTERNS:
            text = pattern.sub(replacement, text)
        text = CITATION_MARKER_PATTERN.sub(
            lambda match: f"[ID:{int(match.group(1) or match.group(2))}]"
            if _marker_allowed(text, match) else match.group(0), text,
        )
        if text == previous:
            return text


def sanitize_citation_markers(answer: str) -> str:
    """Repair unambiguous citations while preserving technical source text."""
    return "".join(
        part if protected else _sanitize_prose(part)
        for protected, part in _prose_segments(answer or "")
    )


def cited_chunk_indexes(answer: str) -> list[int]:
    """Extract only prose citations using the same protection and boundaries."""
    ordered: list[int] = []
    for protected, part in _prose_segments(answer or ""):
        if protected:
            continue
        text = _sanitize_prose(part)
        markers = list(CITATION_MARKER_PATTERN.finditer(text))
        matches = [(m.start(), int(m.group(1) or m.group(2)))
                   for m in markers if _marker_allowed(text, m)]
        # Bare ID:n compatibility must not rescan a bracket marker that was
        # rejected or introduce an extra association inside protected text.
        matches.extend((m.start(), int(m.group(1))) for m in _PROSE_ID_PATTERN.finditer(text)
                       if not any(a.start() <= m.start() < a.end() for a in markers))
        for _, index in sorted(matches):
            if index not in ordered:
                ordered.append(index)
    return ordered


def answer_signals_abstain(answer: str) -> bool:
    """True when the user-facing answer asserts the asked fact is unavailable."""
    return bool(_ABSTAIN_SIGNAL_RE.search(answer or ""))


def chunk_overlaps_answer(chunk: dict, answer: str) -> bool:
    """True when distinctive chunk text also appears in the user-facing answer."""
    content = re.sub(r"\s+", "", str((chunk or {}).get("content") or ""))
    body = re.sub(r"\s+", "", answer or "")
    if len(content) < 6 or len(body) < 6:
        return False
    hits = 0
    seen: set[str] = set()
    limit = min(len(content) - 5, 400)
    for i in range(0, limit, 3):
        frag = content[i : i + 6]
        if not _CONTENT_FRAG_RE.fullmatch(frag):
            continue
        if frag in seen:
            continue
        seen.add(frag)
        if frag in body:
            hits += 1
            if hits >= 2:
                return True
    return hits >= 1


def select_cited_chunk_refs(
    answer: str,
    chunks: list[dict],
    status: str,
) -> list[tuple[dict, int | None]]:
    """Return ``(chunk, refIndex)`` pairs for cited evidence.

    ``refIndex`` is the ``n`` from answer markers ``[ID:n]`` / ``[n]`` / prose
    ``ID:n`` when the chunk was selected by that marker. Overlap fallback (when
    every marker is out of range) returns ``refIndex=None`` so clients must not
    invent an inline binding.
    """
    del status  # Citation evidence is independent from the message business state.
    indexes = cited_chunk_indexes(answer)
    selected: list[tuple[dict, int | None]] = []
    citation_map: dict[int, dict] = {}
    has_explicit_ids = any("citation_id" in chunk for chunk in chunks)
    for chunk in chunks:
        try:
            citation_map[int(chunk["citation_id"])] = chunk
        except (KeyError, TypeError, ValueError):
            continue
    for index in indexes:
        if has_explicit_ids:
            # Workflow IDs are sparse hashes. Never fall back to list position:
            # a missing ID 1 must not silently cite the second unrelated chunk.
            if index in citation_map:
                selected.append((citation_map[index], index))
        elif 0 <= index < len(chunks):
            selected.append((chunks[index], index))
    if selected or has_explicit_ids:
        return selected
    if indexes and chunks and not answer_signals_abstain(answer):
        overlapped = [
            chunk
            for chunk in chunks
            if isinstance(chunk, dict) and chunk_overlaps_answer(chunk, answer)
        ]
        if overlapped and len(overlapped) <= 2:
            return [(chunk, None) for chunk in overlapped]
    return selected


def select_cited_chunks(
    answer: str,
    chunks: list[dict],
    status: str,
) -> list[dict]:
    """Return cited chunks independently from the message business state."""
    return [chunk for chunk, _ref in select_cited_chunk_refs(answer, chunks, status)]
