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
_INVENTORY_QUESTION_RE = re.compile(
    r"(有哪些|有什么|现有|目前有).{0,12}(信息|资料|文档|文件|内容)|哪些资料|哪些文档"
)
_DOCUMENT_TYPE_RE = re.compile(
    r"发票|收据|合格证|调试记录|手册|说明书|工单|验收(?:单|记录)|图纸|合同|移交单"
)
_CATALOG_TYPE_HINTS = (
    (("invoice", "发票"), "发票"),
    (("receipt", "收据"), "收据"),
    (("manual", "handbook", "手册", "说明书"), "手册"),
    (("certificate", "合格证"), "合格证"),
    (("commission", "debug", "调试"), "调试记录"),
    (("workorder", "work-order", "工单"), "工单"),
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
_TIME_BRACKET_DIGIT_RE = re.compile(r":\[(\d+)\]")
_TIME_BRACKET_DIGIT_PREFIX_RE = re.compile(r"\[(\d+)\]:")
_EMPTY_BRACKET_RE = re.compile(r"\[\s*\]")
_CANONICAL_ID_MARKER_RE = re.compile(r"\[ID:(\d+)\]")
_PLACEHOLDER_RE = re.compile(r"\x00CITE(\d+)\x00")
_LOOSE_BRACKET_GROUP_RE = re.compile(r"\[[^\[\]]*\]")
_DANGLING_ID_OPEN_RE = re.compile(r"\[+\s*ID\s*:?", re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def sanitize_citation_markers(answer: str) -> str:
    """Repair or drop mangled ``[ID:n]`` markers; keep only canonical forms.

    Does not change non-marker prose. Time-like ``12:[7]:5`` / ``15:02:[1]``
    lose the brackets around digits. Unrecoverable citation garbage is removed
    so EAM can bind remaining markers via ``refIndex``.
    """
    text = answer or ""
    if "[" not in text:
        return text

    # 1) Strip digit brackets that sit in clock/time fragments first.
    text = _TIME_BRACKET_DIGIT_RE.sub(r":\1", text)
    text = _TIME_BRACKET_DIGIT_PREFIX_RE.sub(r"\1:", text)
    text = _EMPTY_BRACKET_RE.sub("", text)

    # 2) Repair known mangled citation spellings.
    for pattern, repl in _MARKER_REPAIR_PATTERNS:
        text = pattern.sub(repl, text)

    # 3) Canonicalize remaining valid [n] / [ID:n] → [ID:n].
    text = CITATION_MARKER_PATTERN.sub(lambda m: f"[ID:{int(m.group(1))}]", text)

    # 4) Protect canonical markers, strip leftover bracket junk, restore.
    held: list[str] = []

    def _hold(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return f"\x00CITE{len(held) - 1}\x00"

    text = _CANONICAL_ID_MARKER_RE.sub(_hold, text)
    while True:
        nxt = _LOOSE_BRACKET_GROUP_RE.sub("", text)
        if nxt == text:
            break
        text = nxt
    text = _DANGLING_ID_OPEN_RE.sub("", text)
    text = text.replace("[", "").replace("]", "")
    text = _PLACEHOLDER_RE.sub(lambda m: held[int(m.group(1))], text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text



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


def is_inventory_question(question: str) -> bool:
    """True when the user is asking what documents/information currently exist."""
    return bool(_INVENTORY_QUESTION_RE.search(question or ""))


def answer_lists_available_documents(answer: str) -> bool:
    """True when the answer names concrete document types or cites a chunk."""
    text = answer or ""
    return bool(_DOCUMENT_TYPE_RE.search(text) or CITATION_MARKER_PATTERN.search(text))


def catalog_document_types(*labels: str) -> list[str]:
    """Map registry file names / types onto coarse labels without identifiers."""
    found: list[str] = []
    for label in labels:
        blob = str(label or "").casefold()
        if not blob:
            continue
        matched = "文档"
        for hints, name in _CATALOG_TYPE_HINTS:
            if any(hint in blob for hint in hints):
                matched = name
                break
        if matched not in found:
            found.append(matched)
    return found


def catalog_inventory_answer(*labels: str) -> str:
    """Build a Guard-safe inventory sentence from document catalog labels."""
    types = catalog_document_types(*labels)
    if not types:
        return ""
    return "当前知识库中该设备已有以下资料：" + "、".join(types) + "。"


def force_abstain_outcome(answer: str, status: str, question: str = "") -> str:
    """Force no_reliable_evidence when the answer abstains from the user ask.

    Inventory questions ("有哪些信息/资料") may mix a leftover abstain phrase
    with a real listing of retrieved document types. Keep those completed so
    Gateway does not wipe the listing. Repair/fault questions still fail closed.
    """
    if status == "failed":
        return status
    if (
        is_inventory_question(question)
        and answer_lists_available_documents(answer)
    ):
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
    for index in indexes:
        if 0 <= index < len(chunks):
            selected.append((chunks[index], index))
    if selected:
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
