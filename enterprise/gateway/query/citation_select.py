"""Keep only chunks the model actually cited in the answer."""

from __future__ import annotations

import re

# Same marker grammar as RAGFlow dialog_service.CITATION_MARKER_PATTERN.
CITATION_MARKER_PATTERN = re.compile(r"\[(?:ID:)?([0-9\u0660-\u0669\u06F0-\u06F9]+)\]")
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


def cited_chunk_indexes(answer: str) -> list[int]:
    """Return first-seen chunk indexes cited as [ID:n]/[n] or prose ID:n."""
    seen: set[int] = set()
    ordered: list[int] = []
    text = answer or ""
    for pattern in (CITATION_MARKER_PATTERN, _PROSE_ID_PATTERN):
        for match in pattern.finditer(text):
            try:
                index = int(match.group(1))
            except ValueError:
                continue
            if index in seen:
                continue
            seen.add(index)
            ordered.append(index)
    return ordered


def answer_signals_abstain(answer: str) -> bool:
    """True when the user-facing answer asserts the asked fact is unavailable."""
    return bool(_ABSTAIN_SIGNAL_RE.search(answer or ""))


def force_abstain_outcome(answer: str, status: str) -> str:
    """Force no_reliable_evidence when the answer abstains from the user ask."""
    if status == "failed":
        return status
    if answer_signals_abstain(answer):
        return "no_reliable_evidence"
    return status


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


def select_cited_chunks(
    answer: str,
    chunks: list[dict],
    status: str,
) -> list[dict]:
    """Return cited chunks, or none when the run has no reliable evidence.

    Multi-turn answers often reuse prior-turn ``ID:n`` numbers after retrieval
    reorders/shrinks the current chunk list. When every cited index is out of
    range, only keep current-turn chunks whose content actually overlaps the
    answer — never attach unrelated retrieval hits just because IDs drifted.
    """
    if status == "no_reliable_evidence":
        return []
    indexes = cited_chunk_indexes(answer)
    selected: list[dict] = []
    for index in indexes:
        if 0 <= index < len(chunks):
            selected.append(chunks[index])
    if selected:
        return selected
    if indexes and chunks and not answer_signals_abstain(answer):
        overlapped = [
            chunk
            for chunk in chunks
            if isinstance(chunk, dict) and chunk_overlaps_answer(chunk, answer)
        ]
        if overlapped and len(overlapped) <= 2:
            return overlapped
    return selected
