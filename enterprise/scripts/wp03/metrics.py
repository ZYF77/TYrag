"""Pure metric computations for WP-03 parsing quality evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable


GARBLED_CHARS = {
    "\ufffd",  # replacement character
    "\u25a1",  # white square
    "\u25af",  # white vertical rectangle
    "\u0000",  # NUL
    "\ufffe",  # noncharacter
    "\uffff",  # noncharacter
}

_NON_GARBLED_CONTROLS = frozenset("\n\r\t")


def _is_garbled_char(ch: str) -> bool:
    if ch in GARBLED_CHARS:
        return True
    return ch not in _NON_GARBLED_CONTROLS and unicodedata.category(ch) == "Cc"


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def char_length(content: str | None) -> int:
    return len(content or "")


def chunk_page_numbers(chunks: Iterable[dict[str, Any]]) -> set[int]:
    pages: set[int] = set()
    for chunk in chunks:
        for pos in chunk.get("positions") or []:
            try:
                page = int(pos[0])
            except (TypeError, ValueError, IndexError):
                continue
            if page >= 1:
                pages.add(page)
    return pages


def _page_from_position(pos: Any) -> int | None:
    if not isinstance(pos, (list, tuple)) or len(pos) < 1:
        return None
    try:
        return int(pos[0])
    except (TypeError, ValueError, IndexError):
        return None


def in_range_page_numbers(
    chunks: Iterable[dict[str, Any]], source_page_count: int
) -> set[int]:
    pages: set[int] = set()
    for chunk in chunks:
        for pos in chunk.get("positions") or []:
            page = _page_from_position(pos)
            if page is not None and 1 <= page <= source_page_count:
                pages.add(page)
    return pages


def out_of_range_page_positions(
    chunks: Iterable[dict[str, Any]], source_page_count: int
) -> tuple[int, list[int]]:
    count = 0
    pages: set[int] = set()
    for chunk in chunks:
        for pos in chunk.get("positions") or []:
            page = _page_from_position(pos)
            if page is None:
                continue
            if page < 1 or page > source_page_count:
                count += 1
                pages.add(page)
    return count, sorted(pages)


def valid_position_count(chunks: Iterable[dict[str, Any]]) -> int:
    count = 0
    for chunk in chunks:
        positions = chunk.get("positions") or []
        if any(
            _is_valid_position(pos)
            for pos in positions
        ):
            count += 1
    return count


def _is_valid_position(pos: Any) -> bool:
    if not isinstance(pos, (list, tuple)) or len(pos) < 1:
        return False
    try:
        page = int(pos[0])
        if page < 1:
            return False
        values = [float(v) for v in pos[1:5]]
        return all(math.isfinite(v) for v in values)
    except (TypeError, ValueError, IndexError):
        return False


def looks_like_table(chunk: dict[str, Any]) -> bool:
    if str(chunk.get("doc_type_kwd") or "").lower() == "table":
        return True
    content = chunk.get("content") or ""
    if "|---" in content or "\n|" in content:
        return True
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return (
        len(lines) >= 3
        and all("|" in line for line in lines[:3])
    )


def detected_table_pages(chunks: Iterable[dict[str, Any]]) -> set[int]:
    pages: set[int] = set()
    for chunk in chunks:
        if not looks_like_table(chunk):
            continue
        for page in chunk_page_numbers([chunk]):
            pages.add(page)
    return pages


def _field_matches(
    ground_truth_fields: dict[str, str] | None,
    chunks: list[dict[str, Any]],
) -> dict[str, bool]:
    if not ground_truth_fields:
        return {}
    haystack = normalize_text(
        "\n".join(str(c.get("content") or "") for c in chunks)
    )
    matches: dict[str, bool] = {}
    for field, expected in ground_truth_fields.items():
        if expected is None or str(expected).strip() == "":
            continue
        needle = normalize_text(str(expected))
        matches[field] = bool(needle) and needle in haystack
    return matches


def _best_substring_ratio(needle: str, haystack: str) -> float:
    """Best fuzzy match ratio between a field value and chunk text."""
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(haystack) <= len(needle):
        return round(SequenceMatcher(None, needle, haystack).ratio(), 4)
    best = 0.0
    max_len = min(len(needle) + 4, len(haystack))
    step = max(1, len(haystack) // 20_000)
    for width in range(max(1, len(needle) - 2), max_len + 1):
        for i in range(0, len(haystack) - width + 1, step):
            best = max(best, SequenceMatcher(None, needle, haystack[i:i + width]).ratio())
            if best >= 0.999:
                return round(best, 4)
    return round(best, 4)


def _field_similarities(
    ground_truth_fields: dict[str, str] | None,
    chunks: list[dict[str, Any]],
) -> dict[str, float]:
    if not ground_truth_fields:
        return {}
    haystack = normalize_text(
        "\n".join(str(c.get("content") or "") for c in chunks)
    )
    result: dict[str, float] = {}
    for field, expected in ground_truth_fields.items():
        if expected is None or str(expected).strip() == "":
            continue
        result[field] = _best_substring_ratio(normalize_text(str(expected)), haystack)
    return result


def _length_percentiles(lengths: list[int]) -> dict[str, float | int]:
    if not lengths:
        return {
            "min": 0,
            "max": 0,
            "mean": 0.0,
            "p50": 0,
            "p95": 0,
        }
    ordered = sorted(lengths)
    p50 = ordered[len(ordered) // 2] if len(ordered) % 2 else int(
        statistics.mean(ordered[len(ordered) // 2 - 1 : len(ordered) // 2 + 1])
    )
    idx95 = min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)
    return {
        "min": ordered[0],
        "max": ordered[-1],
        "mean": round(statistics.mean(lengths), 2),
        "p50": p50,
        "p95": ordered[idx95],
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def compute_document_metrics(
    doc_info: dict[str, Any] | None,
    chunks: list[dict[str, Any]],
    source_page_count: int,
    ground_truth_fields: dict[str, str] | None = None,
    expected_tables: list[int] | None = None,
    wall_clock_duration_seconds: float | None = None,
    citation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc_info = doc_info or {}
    chunks = list(chunks)
    run = str(doc_info.get("run") or "")
    parse_success = run.upper() in ("DONE", "3")
    parsing_status = run or "UNKNOWN"
    error_code = doc_info.get("error_code")
    if error_code is None and not parse_success:
        error_code = parsing_status

    page_numbers = in_range_page_numbers(chunks, source_page_count)
    out_of_range_count, out_of_range_pages = out_of_range_page_positions(
        chunks, source_page_count
    )
    covered_pages = len(page_numbers)
    empty_page_ratio = (
        max(0.0, 1.0 - covered_pages / source_page_count)
        if source_page_count > 0
        else 0.0
    )
    non_empty = [c for c in chunks if char_length(c.get("content")) > 0]
    total_chars = sum(char_length(c.get("content")) for c in chunks)
    garbled_chars = sum(
        1
        for c in chunks
        for ch in str(c.get("content") or "")
        if _is_garbled_char(ch)
    )
    lengths = [char_length(c.get("content")) for c in chunks]
    table_pages = detected_table_pages(chunks) & set(
        range(1, source_page_count + 1)
    )
    expected_tables = expected_tables or []
    table_recall = (
        round(len(table_pages & set(expected_tables)) / len(set(expected_tables)), 4)
        if expected_tables
        else None
    )
    field_matches = _field_matches(ground_truth_fields, chunks)
    field_similarities = _field_similarities(ground_truth_fields, chunks)
    key_field_accuracy = (
        round(
            sum(field_matches.values()) / len(field_matches),
            4,
        )
        if field_matches
        else None
    )
    key_field_char_similarity = (
        round(
            sum(field_similarities.values()) / len(field_similarities),
            4,
        )
        if field_similarities
        else None
    )

    citation_accuracy: float | None = None
    citation_match_count = 0
    if citation_results:
        matches = [
            result.get("matched", False)
            for result in citation_results
            if result.get("expected_page") is not None
        ]
        if matches:
            citation_match_count = sum(matches)
            citation_accuracy = round(citation_match_count / len(matches), 4)

    process_duration = _safe_float(doc_info.get("process_duration"))
    return {
        "document_id": doc_info.get("id"),
        "dataset_id": doc_info.get("dataset_id") or doc_info.get("kb_id"),
        "parsing_status": parsing_status,
        "error_code": error_code,
        "parse_success": parse_success,
        "api_chunk_count": doc_info.get("chunk_count"),
        "chunk_count": len(chunks),
        "token_count": doc_info.get("token_count"),
        "page_count_source": source_page_count,
        "page_count_observed": covered_pages,
        "out_of_range_page_count": out_of_range_count,
        "out_of_range_pages": out_of_range_pages,
        "empty_page_ratio": round(empty_page_ratio, 4),
        "page_coverage": (
            round(covered_pages / source_page_count, 4)
            if source_page_count > 0
            else 0.0
        ),
        "effective_text_coverage": (
            round(len(non_empty) / len(chunks), 4) if chunks else 0.0
        ),
        "total_chars": total_chars,
        "garbled_chars": garbled_chars,
        "garbled_char_ratio": (
            round(garbled_chars / total_chars, 6) if total_chars else 0.0
        ),
        "empty_chunk_count": len(chunks) - len(non_empty),
        "position_coverage": (
            round(valid_position_count(chunks) / len(chunks), 4) if chunks else 0.0
        ),
        "chunk_length_distribution": _length_percentiles(lengths),
        "table_chunk_count": sum(1 for c in chunks if looks_like_table(c)),
        "image_chunk_count": sum(
            1
            for c in chunks
            if str(c.get("doc_type_kwd") or "").lower() == "image"
        ),
        "table_pages_observed": sorted(table_pages),
        "table_recall": table_recall,
        "key_field_matches": field_matches,
        "key_field_accuracy": key_field_accuracy,
        "key_field_similarities": field_similarities,
        "key_field_char_similarity": key_field_char_similarity,
        "citation_match_count": citation_match_count,
        "citation_page_accuracy": citation_accuracy,
        "parse_duration_seconds": round(process_duration, 3),
        "parse_duration_per_page": (
            round(process_duration / source_page_count, 3)
            if source_page_count > 0
            else 0.0
        ),
        "wall_clock_duration_seconds": (
            round(wall_clock_duration_seconds, 3)
            if wall_clock_duration_seconds is not None
            else None
        ),
        "progress": doc_info.get("progress"),
        "progress_msg": doc_info.get("progress_msg"),
        "parser_profile": doc_info.get("chunk_method") or doc_info.get("parser_id"),
        "parser_version": doc_info.get("parser_version"),
    }


def metrics_hash(documents: list[dict[str, Any]]) -> str:
    stable = json.dumps(
        documents,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


_REPEAT_EXCLUDED = {
    "document_id",
    "dataset_id",
    "task_id",
    "event_id",
    "run_id",
    "created_at",
    "updated_at",
    "parse_duration_seconds",
    "parse_duration_per_page",
    "wall_clock_duration_seconds",
    "progress",
    "progress_msg",
}

_PARSE_EXCLUDED = _REPEAT_EXCLUDED | {
    "quality_status",
    "citation_match_count",
    "citation_page_accuracy",
}


def _normalized_chunk_signature(chunk: dict[str, Any]) -> dict[str, Any]:
    positions: list[list[float]] = []
    for pos in chunk.get("positions") or []:
        page = _page_from_position(pos)
        if page is None:
            continue
        try:
            coords = [round(float(v), 4) for v in pos[1:5]]
        except (TypeError, ValueError, IndexError):
            coords = []
        positions.append([page, *coords])
    return {
        "content": normalize_text(str(chunk.get("content") or "")),
        "positions": positions,
        "doc_type_kwd": str(chunk.get("doc_type_kwd") or "").lower(),
    }


def _stable_result_payload(
    result: dict[str, Any], include_e2e: bool
) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    excluded = _REPEAT_EXCLUDED if include_e2e else _PARSE_EXCLUDED
    payload: dict[str, Any] = {
        "sample_id": result.get("sample_id"),
        "file_sha256": metrics.get("file_sha256"),
        "metrics": {k: v for k, v in metrics.items() if k not in excluded},
        "chunks": [
            _normalized_chunk_signature(chunk)
            for chunk in result.get("chunks") or []
        ],
    }
    if include_e2e:
        payload["quality_reasons"] = result.get("quality_reasons")
        payload["sync_status"] = result.get("sync_status")
    return payload


def _sha256_json(value: Any) -> str:
    stable = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def parse_repeatability_hash(results: list[dict[str, Any]]) -> str:
    """Stable hash for parser reproducibility, excluding e2e citation/status."""
    return _sha256_json(
        {"documents": [_stable_result_payload(result, False) for result in results]}
    )


def e2e_repeatability_hash(results: list[dict[str, Any]]) -> str:
    """Stable hash for end-to-end reproducibility, including citations/status."""
    return _sha256_json(
        {"documents": [_stable_result_payload(result, True) for result in results]}
    )


def repeatability_hash(
    documents: list[dict[str, Any]],
    chunks: list[list[dict[str, Any]]] | None = None,
    *,
    results: list[dict[str, Any]] | None = None,
) -> str:
    """Stable hash excluding runtime IDs, timestamps, and durations."""
    if results is not None:
        return e2e_repeatability_hash(results)
    pseudo_results = [
        {
            "sample_id": None,
            "metrics": doc,
            "quality_reasons": None,
            "sync_status": None,
            "chunks": chunks[index] if chunks is not None and index < len(chunks) else None,
        }
        for index, doc in enumerate(documents)
    ]
    return e2e_repeatability_hash(pseudo_results)


def summarize_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(documents)
    passed = sum(1 for d in documents if d.get("quality_status") == "passed")
    review = sum(
        1 for d in documents if d.get("quality_status") == "review_required"
    )
    failed = sum(1 for d in documents if d.get("quality_status") == "failed")
    parse_success = sum(1 for d in documents if d.get("parse_success"))
    durations = [
        d.get("parse_duration_seconds")
        for d in documents
        if d.get("parse_duration_seconds") is not None
    ]
    wall = [
        d.get("wall_clock_duration_seconds")
        for d in documents
        if d.get("wall_clock_duration_seconds") is not None
    ]
    return {
        "document_count": total,
        "parse_success_count": parse_success,
        "parse_success_rate": round(parse_success / total, 4) if total else 0.0,
        "passed_count": passed,
        "review_required_count": review,
        "failed_count": failed,
        "passed_rate": round(passed / total, 4) if total else 0.0,
        "avg_parse_duration_seconds": (
            round(sum(durations) / len(durations), 3) if durations else None
        ),
        "avg_wall_clock_duration_seconds": (
            round(sum(wall) / len(wall), 3) if wall else None
        ),
    }
