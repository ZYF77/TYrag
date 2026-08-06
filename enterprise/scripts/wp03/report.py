"""Machine and human readable report writers for WP-03 evaluation."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def json_digest(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return json_digest(manifest)


def _page_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        metrics = result.get("metrics") or {}
        source_pages = int(metrics.get("page_count_source") or 0)
        chunks = result.get("chunks") or []
        page_map: dict[int, dict[str, dict[str, Any]]] = {}
        for chunk in chunks:
            for pos in chunk.get("positions") or []:
                try:
                    page = int(pos[0])
                except (TypeError, ValueError, IndexError):
                    continue
                if page >= 1:
                    chunk_key = str(chunk.get("id") or id(chunk))
                    page_map.setdefault(page, {})[chunk_key] = chunk
        for page_no in range(1, source_pages + 1):
            page_chunks = list(page_map.get(page_no, {}).values())
            rows.append(
                {
                    "sample_id": result["sample_id"],
                    "page_no": page_no,
                    "has_chunk": bool(page_chunks),
                    "chunk_count": len(page_chunks),
                    "has_position": any(
                        chunk.get("positions") for chunk in page_chunks
                    ),
                    "char_count": sum(
                        len(str(chunk.get("content") or "")) for chunk in page_chunks
                    ),
                }
            )
    return rows


def _chunk_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        for chunk in result.get("chunks") or []:
            page_no = None
            for pos in chunk.get("positions") or []:
                try:
                    page_no = int(pos[0])
                except (TypeError, ValueError, IndexError):
                    continue
                break
            content = str(chunk.get("content") or "")
            rows.append(
                {
                    "sample_id": result["sample_id"],
                    "chunk_id": chunk.get("id"),
                    "page_no": page_no,
                    "char_length": len(content),
                    "has_position": bool(chunk.get("positions")),
                    "doc_type_kwd": chunk.get("doc_type_kwd"),
                    "image_id": chunk.get("image_id"),
                    "content_preview": content[:200],
                }
            )
    return rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def _document_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        m = result.get("metrics") or {}
        rows.append(
            {
                "sample_id": result["sample_id"],
                "category": result["category"],
                "sync_status": result.get("sync_status"),
                "parse_quality_status": result.get("parse_quality_status"),
                "quality_reasons": ";".join(result.get("quality_reasons") or []),
                "chunk_count": m.get("chunk_count"),
                "page_coverage": m.get("page_coverage"),
                "empty_page_ratio": m.get("empty_page_ratio"),
                "effective_text_coverage": m.get("effective_text_coverage"),
                "garbled_char_ratio": m.get("garbled_char_ratio"),
                "position_coverage": m.get("position_coverage"),
                "error_code": m.get("error_code"),
                "out_of_range_page_count": m.get("out_of_range_page_count"),
                "out_of_range_pages": ";".join(
                    str(page) for page in (m.get("out_of_range_pages") or [])
                ),
                "parser_version": m.get("parser_version"),
                "table_recall": m.get("table_recall"),
                "key_field_accuracy": m.get("key_field_accuracy"),
                "key_field_char_similarity": m.get("key_field_char_similarity"),
                "citation_page_accuracy": m.get("citation_page_accuracy"),
                "parse_duration_seconds": m.get("parse_duration_seconds"),
                "wall_clock_duration_seconds": m.get("wall_clock_duration_seconds"),
            }
        )
    return rows


def _baseline_markdown(
    run_id: str,
    manifest: dict[str, Any],
    thresholds: dict[str, Any],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    environment: dict[str, str],
    command: str,
) -> str:
    rows = _document_rows(results)
    citation_samples = {
        sample["sample_id"]
        for sample in manifest.get("samples", [])
        if sample.get("citation_questions")
    }
    citation_metrics = [
        (result.get("metrics") or {})
        for result in results
        if result.get("sample_id") in citation_samples
    ]
    citation_matched = sum(
        1
        for metrics in citation_metrics
        if metrics.get("citation_page_accuracy") == 1.0
    )
    thresholds_digest = environment.get("thresholds_digest") or json_digest(
        thresholds
    )
    lines = [
        "# WP-03 解析质量基线报告",
        "",
        f"- Run ID: `{run_id}`",
        f"- 生成时间: {datetime.now(timezone.utc).isoformat()}",
        f"- Manifest digest: `{_manifest_digest(manifest)}`",
        f"- Thresholds digest: `{thresholds_digest}`",
        f"- Artifact hash: `{summary.get('artifact_hash', '')}`",
        f"- 样本数: {len(manifest.get('samples', []))}",
        f"- 执行样本数: {len(results)}",
        f"- Ground truth source: {_md_escape((manifest.get('ground_truth_provenance') or {}).get('source', 'unknown'))}",
        f"- Human reviewed: {_md_escape((manifest.get('ground_truth_provenance') or {}).get('human_reviewed', 'unknown'))}",
        f"- 基线性质: {_md_escape(environment.get('baseline_classification', 'unknown'))}",
        f"- Enterprise commit: `{_md_escape(environment.get('enterprise_commit', 'unknown'))}`",
        f"- Enterprise worktree dirty: {_md_escape(environment.get('enterprise_worktree_dirty', 'unknown'))}",
        "- 范围说明：本基线只评测合成样本的文本质量；图片/流程图语义为 `not_evaluated`，diagram `passed` 不表示图形语义通过。",
        "- Ground Truth 与样本生成器同源（`human_reviewed=false`），不是独立人工标注。",
        "- 本基线为脱敏合成工程基线，不代表客户真实扫描档案的解析准确率。",
        f"- Citation {citation_matched}/{len(citation_samples)} 仅代表 {len(citation_samples)} 个已标注案例，不代表 28 份文档整体准确率。",
        f"- 解析成功率: {summary.get('parse_success_rate')}",
        f"- passed: {summary.get('passed_count')}",
        f"- review_required: {summary.get('review_required_count')}",
        f"- failed: {summary.get('failed_count')}",
        f"- Metrics hash: `{summary.get('metrics_hash', '')}`",
        f"- Repeatability hash: `{summary.get('repeatability_hash', '')}`",
        f"- Parse repeatability hash: `{summary.get('parse_repeatability_hash', '')}`",
        f"- E2E repeatability hash: `{summary.get('e2e_repeatability_hash', '')}`",
        f"- 原始解析 Run ID: {_md_escape((summary.get('recompute') or {}).get('original_parse_run_id', run_id))}",
        f"- 是否重新解析: {_md_escape((summary.get('recompute') or {}).get('reparsed', True))}",
        f"- Recompute commit: {_md_escape((summary.get('recompute') or {}).get('recompute_commit') or environment.get('enterprise_commit', ''))}",
        "",
        "## 环境",
        "",
        "| 项 | 值 |",
        "|---|---|",
    ]
    for key, value in environment.items():
        lines.append(f"| {_md_escape(key)} | {_md_escape(value)} |")
    lines.extend(
        [
            "",
            "## 执行命令",
            "",
            "```text",
            command,
            "```",
            "",
            "## 文档级结果",
            "",
            "| Sample | Category | Sync | Quality | Chunk | PageCov | Empty | Text | Garbled | Pos | Table | Field | CharSim | Citation | ParseSec |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {sample_id} | {category} | {sync_status} | {parse_quality_status} | "
            "{chunk_count} | {page_coverage} | {empty_page_ratio} | "
            "{effective_text_coverage} | {garbled_char_ratio} | {position_coverage} | "
            "{table_recall} | {key_field_accuracy} | {key_field_char_similarity} | "
            "{citation_page_accuracy} | "
            "{parse_duration_seconds} |".format(
                **{key: _md_escape(value) for key, value in row.items()}
            )
        )
    low_quality = [
        r for r in results if r.get("parse_quality_status") != "passed"
    ]
    lines.extend(
        [
            "",
            "## 低质量与失败案例",
            "",
        ]
    )
    if not low_quality:
        lines.append("无。")
    else:
        for result in low_quality:
            lines.append(
                f"- {result['sample_id']}: `{result.get('parse_quality_status')}` "
                f"reasons={_md_escape(';'.join(result.get('quality_reasons') or []))}"
            )
    lines.extend(
        [
            "",
            "## 推荐阈值",
            "",
            "| 指标 | 当前阈值 |",
            "|---|---|",
        ]
    )
    for key, value in thresholds.items():
        lines.append(f"| {_md_escape(key)} | {_md_escape(value)} |")
    lines.extend(
        [
            "",
            "## 后续解析优化优先级",
            "",
            "1. 对 `review_required` 样本先按指标归因，再决定 OCR、表格或图片专项处理。",
            "2. 对 `empty_page_ratio` 高的扫描样本验证 DeepDoc OCR 与页面旋转预处理。",
            "3. 对 `table_recall` 低的表格样本评估表格专用 parser 或人工复核。",
            "4. 对 `position_coverage` 低但文本可读的样本检查 Parser profile 与坐标输出。",
            "5. 扩大人工 ground truth 覆盖后再校准阈值，不提前宣称准确率。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(
    output_dir: str | Path,
    run_id: str,
    manifest: dict[str, Any],
    thresholds: dict[str, Any],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    environment: dict[str, str],
    command: str,
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": _manifest_digest(manifest),
        "thresholds_digest": environment.get("thresholds_digest")
        or json_digest(thresholds),
        "ground_truth_provenance": manifest.get("ground_truth_provenance"),
        "thresholds": thresholds,
        "summary": summary,
        "environment": environment,
        "command": command,
        "documents": results,
        "recompute": summary.get("recompute")
        or {
            "original_parse_run_id": run_id,
            "reparsed": True,
            "recomputed_from_saved_results": False,
        },
    }
    artifact_hash = json_digest(report)
    report["artifact_hash"] = artifact_hash
    summary["artifact_hash"] = artifact_hash
    report_path = output_dir / "evaluation-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    doc_path = output_dir / "documents.csv"
    _write_csv(
        doc_path,
        [
            "sample_id",
            "category",
            "sync_status",
            "parse_quality_status",
            "quality_reasons",
            "chunk_count",
            "page_coverage",
            "empty_page_ratio",
            "effective_text_coverage",
            "garbled_char_ratio",
            "position_coverage",
            "error_code",
            "out_of_range_page_count",
            "out_of_range_pages",
            "parser_version",
            "table_recall",
            "key_field_accuracy",
            "key_field_char_similarity",
            "citation_page_accuracy",
            "parse_duration_seconds",
            "wall_clock_duration_seconds",
        ],
        _document_rows(results),
    )
    pages_path = output_dir / "pages.csv"
    _write_csv(
        pages_path,
        ["sample_id", "page_no", "has_chunk", "chunk_count", "has_position", "char_count"],
        _page_rows(results),
    )
    chunks_path = output_dir / "chunks.csv"
    _write_csv(
        chunks_path,
        [
            "sample_id",
            "chunk_id",
            "page_no",
            "char_length",
            "has_position",
            "doc_type_kwd",
            "image_id",
            "content_preview",
        ],
        _chunk_rows(results),
    )
    md_path = output_dir / "baseline.md"
    md_path.write_text(
        _baseline_markdown(
            run_id,
            manifest,
            thresholds,
            results,
            summary,
            environment,
            command,
        ),
        encoding="utf-8",
    )
    return {
        "json": report_path,
        "documents_csv": doc_path,
        "pages_csv": pages_path,
        "chunks_csv": chunks_path,
        "markdown": md_path,
    }
