"""CLI entrypoint for WP-03 real sample parsing evaluation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.scripts.wp03.collector import (  # noqa: E402
    EvaluationConfig,
    ParsingEvaluationCollector,
    load_manifest,
)
from enterprise.scripts.wp03.metrics import (  # noqa: E402
    e2e_repeatability_hash,
    metrics_hash,
    parse_repeatability_hash,
    summarize_documents,
)
from enterprise.scripts.wp03.quality_gate import load_thresholds  # noqa: E402
from enterprise.scripts.wp03.report import json_digest, write_reports  # noqa: E402


EVALUATION_CONTRACT = "contracts/parse-quality-evaluation.md"
EVALUATION_CONTRACT_VERSION = "1"


def _git_state(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def _classify_baseline(
    worktree_dirty: bool, allow_dirty: bool, commit_unknown: bool = False
) -> str:
    if worktree_dirty and not allow_dirty:
        raise RuntimeError(
            "refusing to create a formal baseline from a dirty worktree; "
            "pass --allow-dirty to mark the run informal"
        )
    if commit_unknown:
        return "informal_unknown_commit"
    if worktree_dirty:
        return "informal_dirty_worktree"
    return "formal"


def _env_dict(
    run_id: str,
    manifest_path: Path,
    thresholds_path: Path,
    samples_dir: Path,
    tenant_id: str | None = None,
) -> dict[str, str]:
    version_manifest = {}
    version_path = ROOT / "version-manifest.json"
    if version_path.exists():
        with open(version_path, encoding="utf-8") as f:
            version_manifest = json.load(f)
    upstream = version_manifest.get("ragflow_upstream", {})
    enterprise_commit, enterprise_worktree_dirty = _git_state(ROOT)
    return {
        "run_id": run_id,
        "gateway_url": os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188"),
        "ragflow_base_url": os.environ.get(
            "RAGFLOW_BASE_URL", "http://127.0.0.1:9380"
        ),
        "s3_endpoint": os.environ.get("S3_ENDPOINT", ""),
        "s3_bucket": os.environ.get("S3_BUCKET", "wp03-eval"),
        "tenant_id": tenant_id or os.environ.get("WP03_TENANT", "wp03-eval"),
        "ragflow_source_tag": upstream.get("source_tag", "unknown"),
        "ragflow_source_commit": upstream.get("source_commit", "unknown"),
        "enterprise_commit": enterprise_commit,
        "enterprise_worktree_dirty": enterprise_worktree_dirty,
        "manifest_digest": json_digest(
            json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        ),
        "thresholds_digest": json_digest(
            json.loads(Path(thresholds_path).read_text(encoding="utf-8"))
        ),
        "evaluation_contract": EVALUATION_CONTRACT,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "manifest": str(manifest_path),
        "thresholds": str(thresholds_path),
        "samples_dir": str(samples_dir),
    }


def _redacted_command(run_id: str, args: argparse.Namespace) -> str:
    parts = [
        "python enterprise/scripts/wp03/run_parsing_evaluation.py",
        f"--run-id {run_id}",
        f"--manifest {args.manifest}",
        f"--samples-dir {args.samples_dir}",
        f"--thresholds {args.thresholds}",
        f"--output-dir {args.output_dir}",
    ]
    if args.limit:
        parts.append(f"--limit {args.limit}")
    if args.only:
        parts.append("--only <redacted>")
    if args.skip_citations:
        parts.append("--skip-citations")
    if args.fresh_parse:
        parts.append("--fresh-parse")
    if args.allow_dirty:
        parts.append("--allow-dirty")
    return " ".join(parts)


async def _run_one_sample(
    collector: ParsingEvaluationCollector,
    sample: dict[str, Any],
    samples_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    try:
        return await collector.run_sample(sample, samples_dir, run_id)
    except Exception as exc:  # noqa: BLE001
        logging.exception("sample failed: %s", sample["sample_id"])
        file_sha256 = None
        try:
            file_sha256 = hashlib.sha256(
                (samples_dir / sample["file_name"]).read_bytes()
            ).hexdigest()
        except (OSError, KeyError, TypeError):
            pass
        return {
            "sample_id": sample["sample_id"],
            "category": sample["category"],
            "sync_status": "failed",
            "parse_quality_status": "failed",
            "quality_reasons": [type(exc).__name__],
            "metrics": {
                "document_id": None,
                "dataset_id": None,
                "file_sha256": file_sha256,
                "parsing_status": "COLLECTION_ERROR",
                "error_code": type(exc).__name__,
                "parse_success": False,
                "chunk_count": 0,
                "page_count_source": int(sample["pages"]),
                "page_count_observed": 0,
                "quality_status": "failed",
            },
            "chunks": [],
            "error": str(exc),
        }


async def _run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    thresholds = load_thresholds(args.thresholds)
    run_id = args.run_id or f"wp03-{uuid.uuid4().hex[:12]}"
    samples_dir = Path(args.samples_dir)
    output_dir = Path(args.output_dir) / run_id
    tenant_id = args.tenant
    if args.fresh_parse:
        tenant_id = f"{args.tenant}-{run_id}"

    config = EvaluationConfig(
        gateway_url=args.gateway_url,
        ragflow_base_url=args.ragflow_base_url,
        ragflow_api_key=args.ragflow_api_key,
        service_token=args.service_token,
        s3_endpoint=args.s3_endpoint,
        s3_access_key=args.s3_access_key,
        s3_secret_key=args.s3_secret_key,
        s3_bucket=args.s3_bucket,
        tenant_id=tenant_id,
        timeout_seconds=args.timeout,
        skip_citations=args.skip_citations,
        fresh_parse=args.fresh_parse,
    )
    if not (config.s3_endpoint and config.s3_access_key and config.s3_secret_key):
        raise RuntimeError(
            "S3_ENDPOINT/S3_ACCESS_KEY/S3_SECRET_KEY must be configured"
        )
    if not config.ragflow_api_key:
        raise RuntimeError("RAGFLOW_API_KEY must be configured")

    samples = manifest["samples"]
    if args.only:
        only = set(args.only.split(","))
        samples = [s for s in samples if s["sample_id"] in only]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise RuntimeError("no samples selected")

    collector = ParsingEvaluationCollector(config)
    results: list[dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        print(
            f"[{index}/{len(samples)}] {sample['sample_id']} "
            f"({sample['category']})"
        )
        result = await _run_one_sample(collector, sample, samples_dir, run_id)
        results.append(result)
        status = result.get("parse_quality_status")
        reasons = ";".join(result.get("quality_reasons") or [])
        print(f"  -> sync={result.get('sync_status')} quality={status} reasons={reasons}")

    metrics_list = [result["metrics"] for result in results]
    summary = summarize_documents(metrics_list)
    summary["metrics_hash"] = metrics_hash(metrics_list)
    summary["parse_repeatability_hash"] = parse_repeatability_hash(results)
    summary["e2e_repeatability_hash"] = e2e_repeatability_hash(results)
    summary["repeatability_hash"] = summary["e2e_repeatability_hash"]
    summary["recompute"] = {
        "original_parse_run_id": run_id,
        "reparsed": True,
        "recomputed_from_saved_results": False,
    }
    environment = _env_dict(
        run_id,
        Path(args.manifest),
        Path(args.thresholds),
        samples_dir,
        tenant_id,
    )
    environment["baseline_classification"] = _classify_baseline(
        environment["enterprise_worktree_dirty"],
        args.allow_dirty,
        environment.get("enterprise_commit") in (None, "", "unknown"),
    )
    command = _redacted_command(run_id, args)
    paths = write_reports(
        output_dir,
        run_id,
        manifest,
        thresholds,
        results,
        summary,
        environment,
        command,
    )
    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=2))
    print("reports written:")
    for kind, path in paths.items():
        print(f"  {kind}: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="enterprise/scripts/wp03/sample_manifest.json")
    parser.add_argument("--samples-dir", default="artifacts/wp03/samples")
    parser.add_argument("--thresholds", default="enterprise/scripts/wp03/thresholds.json")
    parser.add_argument("--output-dir", default="artifacts/wp03/reports")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-citations", action="store_true")
    parser.add_argument("--fresh-parse", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--gateway-url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:5188"))
    parser.add_argument("--ragflow-base-url", default=os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380"))
    parser.add_argument("--ragflow-api-key", default=os.environ.get("RAGFLOW_API_KEY", ""))
    parser.add_argument("--service-token", default=os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN", ""))
    parser.add_argument("--s3-endpoint", default=os.environ.get("S3_ENDPOINT", ""))
    parser.add_argument("--s3-access-key", default=os.environ.get("S3_ACCESS_KEY", ""))
    parser.add_argument("--s3-secret-key", default=os.environ.get("S3_SECRET_KEY", ""))
    parser.add_argument("--s3-bucket", default=os.environ.get("S3_BUCKET", "wp03-eval"))
    parser.add_argument("--tenant", default=os.environ.get("WP03_TENANT", "wp03-eval"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
