"""Recompute WP-03 report hashes from saved fresh-parse results."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.scripts.wp03.collector import load_manifest  # noqa: E402
from enterprise.scripts.wp03.metrics import (  # noqa: E402
    e2e_repeatability_hash,
    parse_repeatability_hash,
)
from enterprise.scripts.wp03.quality_gate import load_thresholds  # noqa: E402
from enterprise.scripts.wp03.report import write_reports  # noqa: E402
from enterprise.scripts.wp03.run_parsing_evaluation import _git_state  # noqa: E402


def recompute_report_hashes(
    report: dict[str, Any],
    manifest: dict[str, Any],
    samples_dir: Path,
    recompute_commit: str,
) -> dict[str, Any]:
    docs = report["documents"]
    file_names = {
        sample["sample_id"]: sample["file_name"]
        for sample in manifest["samples"]
    }
    for doc in docs:
        file_name = file_names.get(doc.get("sample_id"))
        sha256 = None
        if file_name:
            try:
                sha256 = hashlib.sha256(
                    (samples_dir / file_name).read_bytes()
                ).hexdigest()
            except OSError:
                pass
        (doc.get("metrics") or {})["file_sha256"] = sha256
    summary = report["summary"]
    summary["parse_repeatability_hash"] = parse_repeatability_hash(docs)
    summary["e2e_repeatability_hash"] = e2e_repeatability_hash(docs)
    summary["repeatability_hash"] = summary["e2e_repeatability_hash"]
    summary["recompute"] = {
        "original_parse_run_id": report.get("run_id"),
        "reparsed": False,
        "recomputed_from_saved_results": True,
        "recompute_commit": recompute_commit,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-ids",
        default="wp03-phase1-full-v2,wp03-phase1-full-v2-repeat",
    )
    parser.add_argument("--reports-root", default="artifacts/wp03/reports")
    parser.add_argument(
        "--manifest", default="enterprise/scripts/wp03/sample_manifest.json"
    )
    parser.add_argument("--samples-dir", default="artifacts/wp03/samples")
    parser.add_argument(
        "--thresholds", default="enterprise/scripts/wp03/thresholds.json"
    )
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    thresholds = load_thresholds(args.thresholds)
    samples_dir = Path(args.samples_dir)
    reports_root = Path(args.reports_root)
    commit, _ = _git_state(ROOT)
    for run_id in [item.strip() for item in args.run_ids.split(",") if item.strip()]:
        report_dir = reports_root / run_id
        report_path = report_dir / "evaluation-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        recompute_report_hashes(report, manifest, samples_dir, commit)
        write_reports(
            report_dir,
            report["run_id"],
            manifest,
            thresholds,
            report["documents"],
            report["summary"],
            report["environment"],
            report["command"],
        )
        print(f"recomputed {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
