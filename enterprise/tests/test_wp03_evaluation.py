"""Unit tests for WP-03 parsing quality metrics, gate, manifest, and reports."""

from __future__ import annotations

import json
import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.scripts.wp03.collector import (  # noqa: E402
    EvaluationConfig,
    ParsingEvaluationCollector,
    validate_manifest,
)
from enterprise.scripts.wp03.metrics import (  # noqa: E402
    compute_document_metrics,
    e2e_repeatability_hash,
    metrics_hash,
    parse_repeatability_hash,
    repeatability_hash,
)
from enterprise.scripts.wp03.quality_gate import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    evaluate_document_quality,
    load_thresholds,
)
from enterprise.scripts.wp03.report import (  # noqa: E402
    _page_rows,
    json_digest,
    write_reports,
)
from enterprise.scripts.wp03.recompute_hashes import recompute_report_hashes  # noqa: E402
from enterprise.scripts.wp03.run_parsing_evaluation import (  # noqa: E402
    _classify_baseline,
    _env_dict,
    _run_one_sample,
)


def _manifest(samples: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "ground_truth_provenance": {
            "source": "synthetic_generator",
            "human_reviewed": False,
        },
        "samples": samples,
    }


def _good_doc() -> dict:
    return {
        "id": "doc-1",
        "dataset_id": "ds-1",
        "run": "DONE",
        "chunk_count": 2,
        "token_count": 20,
        "process_duration": 4.0,
        "chunk_method": "naive",
    }


def _good_chunks() -> list[dict]:
    return [
        {
            "id": "c1",
            "content": "Equipment ID: EQ-2026-0001\nModel: TYR-5001-A",
            "document_id": "doc-1",
            "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
        },
        {
            "id": "c2",
            "content": "Fault Code: E-101\nVersion: V1.1",
            "document_id": "doc-1",
            "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
        },
    ]


def _fields() -> dict:
    return {
        "equipment_id": "EQ-2026-0001",
        "model": "TYR-5001-A",
        "fault_code": "E-101",
    }


class TestMetrics:
    def test_good_document_passes(self):
        metrics = compute_document_metrics(
            _good_doc(),
            _good_chunks(),
            1,
            ground_truth_fields=_fields(),
        )
        status, reasons = evaluate_document_quality(
            metrics,
            ground_truth_fields=_fields(),
        )
        assert metrics["page_coverage"] == 1.0
        assert metrics["position_coverage"] == 1.0
        assert metrics["key_field_accuracy"] == 1.0
        assert status == "passed"
        assert reasons == ["PASSED"]

    def test_done_with_empty_chunks_is_review_required(self):
        metrics = compute_document_metrics(
            {**_good_doc(), "chunk_count": 0},
            [],
            2,
            ground_truth_fields=_fields(),
        )
        status, reasons = evaluate_document_quality(
            metrics,
            ground_truth_fields=_fields(),
        )
        assert status == "review_required"
        assert "CHUNK_COUNT_BELOW_MIN" in reasons
        assert "EMPTY_PAGE_RATIO_ABOVE_MAX" in reasons

    def test_done_does_not_auto_pass(self):
        metrics = compute_document_metrics(
            {**_good_doc(), "run": "DONE"},
            [],
            2,
            ground_truth_fields=_fields(),
        )
        assert metrics["parse_success"] is True
        status, _ = evaluate_document_quality(metrics)
        assert status != "passed"

    def test_failed_parse_is_failed(self):
        metrics = compute_document_metrics(
            {**_good_doc(), "run": "FAIL"},
            _good_chunks(),
            1,
        )
        status, reasons = evaluate_document_quality(metrics)
        assert status == "failed"
        assert reasons == ["RAGFLOW_PARSE_FAILED"]

    def test_table_recall(self):
        chunks = [
            {
                "id": "p1",
                "content": "Intro text for page one",
                "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
            },
            {
                "id": "t1",
                "content": "| A | B |\n|---|---|\n| 1 | 2 |",
                "positions": [[2, 0.1, 0.2, 0.8, 0.4]],
            }
        ]
        metrics = compute_document_metrics(
            _good_doc(),
            chunks,
            2,
            expected_tables=[2],
        )
        assert metrics["table_recall"] == 1.0
        status, _ = evaluate_document_quality(
            metrics,
            expected_tables=[2],
        )
        assert status == "passed"

    def test_key_field_missing_lowers_accuracy(self):
        chunks = [{"id": "c1", "content": "nothing useful", "positions": []}]
        metrics = compute_document_metrics(
            _good_doc(),
            chunks,
            1,
            ground_truth_fields=_fields(),
        )
        assert metrics["key_field_accuracy"] == 0.0
        status, reasons = evaluate_document_quality(
            metrics,
            ground_truth_fields=_fields(),
        )
        assert status == "review_required"
        assert "KEY_FIELD_ACCURACY_BELOW_MIN" in reasons

    def test_key_field_char_similarity_discriminates_ocr_errors(self):
        clean = compute_document_metrics(
            _good_doc(),
            [
                {
                    "id": "c1",
                    "content": "Equipment ID: EQ-2026-0001",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                }
            ],
            1,
            ground_truth_fields={"equipment_id": "EQ-2026-0001"},
        )
        mangled = compute_document_metrics(
            _good_doc(),
            [
                {
                    "id": "c1",
                    "content": "Equipment ID: EQ-2026-OOO1",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                }
            ],
            1,
            ground_truth_fields={"equipment_id": "EQ-2026-0001"},
        )
        assert clean["key_field_char_similarity"] == 1.0
        assert mangled["key_field_char_similarity"] < 1.0
        assert mangled["key_field_char_similarity"] > 0.5

    def test_image_chunk_count_ignores_image_id_on_text_chunks(self):
        metrics = compute_document_metrics(
            _good_doc(),
            [
                {
                    "id": "t1",
                    "content": "text",
                    "image_id": "dataset-t1",
                    "doc_type_kwd": "text",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                },
                {
                    "id": "i1",
                    "content": "labels",
                    "image_id": "dataset-i1",
                    "doc_type_kwd": "image",
                    "positions": [[1, 0.1, 0.2, 0.8, 0.4]],
                },
            ],
            1,
        )
        assert metrics["image_chunk_count"] == 1

    def test_citation_page_accuracy(self):
        metrics = compute_document_metrics(
            _good_doc(),
            _good_chunks(),
            1,
            citation_results=[
                {"expected_page": 1, "matched": True},
                {"expected_page": 2, "matched": False},
            ],
        )
        assert metrics["citation_page_accuracy"] == 0.5
        status, reasons = evaluate_document_quality(
            metrics,
            citation_expected=True,
        )
        assert status == "review_required"
        assert "CITATION_PAGE_ACCURACY_BELOW_MIN" in reasons

    def test_metrics_hash_is_deterministic(self):
        first = metrics_hash(
            [compute_document_metrics(_good_doc(), _good_chunks(), 1)]
        )
        second = metrics_hash(
            [compute_document_metrics(_good_doc(), _good_chunks(), 1)]
        )
        assert first == second

    def test_repeatability_hash_ignores_duration(self):
        first = compute_document_metrics(
            _good_doc(), _good_chunks(), 1, wall_clock_duration_seconds=1.0
        )
        second = compute_document_metrics(
            {**_good_doc(), "process_duration": 99.0},
            _good_chunks(),
            1,
            wall_clock_duration_seconds=55.0,
        )
        assert first["parse_duration_seconds"] != second["parse_duration_seconds"]
        assert repeatability_hash([first]) == repeatability_hash([second])

    def _result_dict(
        self,
        sample_id: str = "s1",
        file_sha256: str = "a" * 64,
        reasons: list[str] | None = None,
    ) -> dict:
        metrics = compute_document_metrics(_good_doc(), _good_chunks(), 1)
        metrics["file_sha256"] = file_sha256
        return {
            "sample_id": sample_id,
            "quality_reasons": reasons or ["PASSED"],
            "sync_status": "ready",
            "metrics": metrics,
            "chunks": _good_chunks(),
        }

    def test_parse_repeatability_hash_changes_when_sample_id_changes(self):
        first = self._result_dict(sample_id="s1")
        second = self._result_dict(sample_id="s2")
        assert parse_repeatability_hash([first]) != parse_repeatability_hash(
            [second]
        )

    def test_e2e_repeatability_hash_changes_when_file_sha256_changes(self):
        first = self._result_dict(file_sha256="a" * 64)
        second = self._result_dict(file_sha256="b" * 64)
        assert e2e_repeatability_hash([first]) != e2e_repeatability_hash(
            [second]
        )

    def test_e2e_repeatability_hash_changes_when_reason_codes_change(self):
        first = self._result_dict(reasons=["PASSED"])
        second = self._result_dict(reasons=["PAGE_COVERAGE_BELOW_MIN"])
        assert e2e_repeatability_hash([first]) != e2e_repeatability_hash(
            [second]
        )

    def test_parse_repeatability_hash_ignores_reason_codes(self):
        first = self._result_dict(reasons=["PASSED"])
        second = self._result_dict(reasons=["PAGE_COVERAGE_BELOW_MIN"])
        assert parse_repeatability_hash([first]) == parse_repeatability_hash(
            [second]
        )

    def test_repeatability_hash_ignores_runtime_resource_ids(self):
        first = compute_document_metrics(
            {**_good_doc(), "id": "doc-a", "dataset_id": "ds-a"},
            _good_chunks(),
            1,
        )
        second = compute_document_metrics(
            {**_good_doc(), "id": "doc-b", "dataset_id": "ds-b"},
            _good_chunks(),
            1,
        )
        first.update(
            {
                "task_id": "task-a",
                "event_id": "event-a",
                "created_at": "2026-01-01",
                "updated_at": "2026-01-02",
            }
        )
        second.update(
            {
                "task_id": "task-b",
                "event_id": "event-b",
                "created_at": "2026-02-01",
                "updated_at": "2026-02-02",
            }
        )
        assert repeatability_hash([first], [_good_chunks()]) == repeatability_hash(
            [second], [_good_chunks()]
        )

    def test_repeatability_hash_changes_when_chunk_content_changes(self):
        changed = [
            {**chunk, "content": "completely different parsed text"}
            for chunk in _good_chunks()
        ]
        first = compute_document_metrics(_good_doc(), _good_chunks(), 1)
        second = compute_document_metrics(_good_doc(), changed, 1)
        assert repeatability_hash([first], [_good_chunks()]) != repeatability_hash(
            [second], [changed]
        )

    def test_repeatability_hash_changes_when_page_number_changes(self):
        chunks_a = [
            {**chunk, "positions": [[1, 0.1, 0.2, 0.8, 0.4]]}
            for chunk in _good_chunks()
        ]
        chunks_b = [
            {**chunk, "positions": [[2, 0.1, 0.2, 0.8, 0.4]]}
            for chunk in _good_chunks()
        ]
        first = compute_document_metrics(_good_doc(), chunks_a, 2)
        second = compute_document_metrics(_good_doc(), chunks_b, 2)
        assert repeatability_hash([first], [chunks_a]) != repeatability_hash(
            [second], [chunks_b]
        )

    def test_out_of_range_positions_do_not_count_as_covered_pages(self):
        chunks = [
            {"id": "bad", "content": "ghost page", "positions": [[99, 0, 0, 1, 1]]},
            {"id": "good", "content": "real page", "positions": [[1, 0, 0, 1, 1]]},
        ]
        metrics = compute_document_metrics(_good_doc(), chunks, 2)
        assert metrics["page_count_observed"] == 1
        assert metrics["page_coverage"] == 0.5
        assert metrics["empty_page_ratio"] == 0.5
        assert metrics["out_of_range_page_count"] == 1
        assert metrics["out_of_range_pages"] == [99]

    def test_out_of_range_pages_cannot_mask_empty_pages_or_exceed_one(self):
        chunks = [
            {"id": "bad", "content": "ghost page", "positions": [[99, 0, 0, 1, 1]]},
            {"id": "bad2", "content": "another ghost", "positions": [[100, 0, 0, 1, 1]]},
        ]
        metrics = compute_document_metrics(_good_doc(), chunks, 2)
        assert metrics["page_count_observed"] == 0
        assert metrics["page_coverage"] == 0.0
        assert metrics["page_coverage"] <= 1.0
        assert metrics["empty_page_ratio"] == 1.0
        assert metrics["out_of_range_page_count"] == 2
        status, reasons = evaluate_document_quality(metrics)
        assert status == "review_required"
        assert "POSITION_PAGE_OUT_OF_RANGE" in reasons


class TestManifestValidation:
    def test_valid_manifest(self):
        manifest = _manifest(
            [
                {
                    "sample_id": "s1",
                    "category": "digital_text",
                    "file_name": "s1.pdf",
                    "pages": 1,
                    "ground_truth_fields": {"equipment_id": "EQ-1"},
                }
            ]
        )
        validate_manifest(manifest)

    def test_duplicate_sample_rejected(self):
        manifest = _manifest(
            [
                {
                    "sample_id": "s1",
                    "category": "digital_text",
                    "pages": 1,
                    "ground_truth_fields": {},
                },
                {
                    "sample_id": "s1",
                    "category": "clear_scan",
                    "pages": 1,
                    "ground_truth_fields": {},
                },
            ]
        )
        with pytest.raises(ValueError):
            validate_manifest(manifest)

    def test_unknown_category_rejected(self):
        manifest = _manifest(
            [
                {
                    "sample_id": "s1",
                    "category": "unknown",
                    "pages": 1,
                    "ground_truth_fields": {},
                }
            ]
        )
        with pytest.raises(ValueError):
            validate_manifest(manifest)

    def test_missing_ground_truth_provenance_rejected(self):
        manifest = {
            "schema_version": 1,
            "samples": [
                {
                    "sample_id": "s1",
                    "category": "digital_text",
                    "pages": 1,
                    "ground_truth_fields": {},
                }
            ],
        }
        with pytest.raises(ValueError):
            validate_manifest(manifest)


class TestReportWriter:
    def test_writes_json_csv_and_markdown(self, tmp_path):
        results = [
            {
                "sample_id": "s1",
                "category": "digital_text",
                "sync_status": "ready",
                "parse_quality_status": "passed",
                "quality_reasons": ["PASSED"],
                "metrics": compute_document_metrics(
                    _good_doc(), _good_chunks(), 1
                ),
                "chunks": _good_chunks(),
            }
        ]
        manifest = {
            "schema_version": 1,
            "name": "test",
            "ground_truth_provenance": {
                "source": "synthetic_generator",
                "human_reviewed": False,
            },
            "samples": [
                {
                    "sample_id": "s1",
                    "category": "digital_text",
                    "file_name": "s1.pdf",
                    "pages": 1,
                    "ground_truth_fields": {},
                }
            ],
        }
        summary = {
            "document_count": 1,
            "passed_count": 1,
            "parse_success_rate": 1.0,
            "artifact_hash": "stale",
        }
        environment = {
            "ragflow_source_tag": "v0.26.4",
            "ragflow_source_commit": "abc123",
            "enterprise_commit": "def456",
            "enterprise_worktree_dirty": False,
            "manifest_digest": "m1",
            "thresholds_digest": "t1",
            "evaluation_contract_version": "1",
            "baseline_classification": "formal",
        }
        paths = write_reports(
            tmp_path,
            "run-test",
            manifest,
            DEFAULT_THRESHOLDS,
            results,
            summary,
            environment,
            "python run_parsing_evaluation.py --run-id run-test",
        )
        assert paths["json"].exists()
        assert paths["documents_csv"].exists()
        assert paths["pages_csv"].exists()
        assert paths["chunks_csv"].exists()
        assert paths["markdown"].exists()
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert report["documents"][0]["parse_quality_status"] == "passed"
        assert report["environment"]["enterprise_commit"] == "def456"
        assert report["environment"]["enterprise_worktree_dirty"] is False
        assert report["thresholds_digest"] == "t1"
        assert report["artifact_hash"]
        report_no_hash = {
            k: v for k, v in report.items() if k != "artifact_hash"
        }
        report_no_hash["summary"] = {
            k: v
            for k, v in report_no_hash["summary"].items()
            if k != "artifact_hash"
        }
        assert json_digest(report_no_hash) == report["artifact_hash"]
        assert report["recompute"]["original_parse_run_id"] == "run-test"
        md = paths["markdown"].read_text(encoding="utf-8")
        assert "Thresholds digest: `t1`" in md
        assert "Citation 0/0" in md

    def test_status_does_not_mutate_sync_status(self):
        sync_status = "ready"
        metrics = compute_document_metrics(_good_doc(), [], 2)
        status, _ = evaluate_document_quality(metrics)
        assert status == "review_required"
        assert sync_status == "ready"

    def test_page_rows_counts_unique_chunks_per_page(self):
        results = [
            {
                "sample_id": "s1",
                "metrics": {"page_count_source": 2},
                "chunks": [
                    {
                        "id": "c1",
                        "content": "abc",
                        "positions": [[1, 0, 0, 1, 1], [1, 0, 0, 1, 1], [2, 0, 0, 1, 1]],
                    }
                ],
            }
        ]
        rows = _page_rows(results)
        page1 = next(row for row in rows if row["page_no"] == 1)
        page2 = next(row for row in rows if row["page_no"] == 2)
        assert page1["chunk_count"] == 1
        assert page1["char_count"] == 3
        assert page2["chunk_count"] == 1
        assert page2["char_count"] == 3


class TestFailureAndConfig:
    def test_recompute_report_hashes_injects_file_sha256_and_metadata(self, tmp_path):
        pdf = tmp_path / "s1.pdf"
        pdf.write_bytes(b"pdf-bytes")
        manifest = _manifest(
            [
                {
                    "sample_id": "s1",
                    "category": "digital_text",
                    "file_name": "s1.pdf",
                    "pages": 1,
                    "ground_truth_fields": {},
                }
            ]
        )
        metrics = compute_document_metrics(_good_doc(), _good_chunks(), 1)
        report = {
            "run_id": "run-x",
            "documents": [
                {
                    "sample_id": "s1",
                    "quality_reasons": ["PASSED"],
                    "sync_status": "ready",
                    "metrics": metrics,
                    "chunks": _good_chunks(),
                }
            ],
            "summary": {},
        }
        recompute_report_hashes(report, manifest, tmp_path, "commit123")
        assert report["documents"][0]["metrics"]["file_sha256"] == hashlib.sha256(
            b"pdf-bytes"
        ).hexdigest()
        assert report["summary"]["metrics_hash"] == metrics_hash(
            [report["documents"][0]["metrics"]]
        )
        assert report["summary"]["recompute"]["original_parse_run_id"] == "run-x"
        assert report["summary"]["recompute"]["reparsed"] is False
        assert report["summary"]["recompute"]["recompute_commit"] == "commit123"

    @pytest.mark.asyncio
    async def test_wait_ragflow_terminal_polls_until_done(self):
        class FakeClient:
            def __init__(self):
                self.responses = [{"run": "RUNNING"}, {"run": "DONE"}]

            async def list_documents(self, dataset_id, document_id):
                return [self.responses.pop(0)]

        collector = ParsingEvaluationCollector(EvaluationConfig())
        doc = await collector._wait_ragflow_terminal(
            FakeClient(),  # type: ignore[arg-type]
            "ds",
            "doc",
            timeout_seconds=30,
            poll_seconds=0,
        )
        assert doc["run"] == "DONE"

    @pytest.mark.asyncio
    async def test_wait_ragflow_terminal_timeout_fails_closed(self):
        class FakeClient:
            async def list_documents(self, dataset_id, document_id):
                return [{"run": "RUNNING"}]

        collector = ParsingEvaluationCollector(EvaluationConfig())
        with pytest.raises(TimeoutError):
            await collector._wait_ragflow_terminal(
                FakeClient(),  # type: ignore[arg-type]
                "ds",
                "doc",
                timeout_seconds=0.01,
                poll_seconds=0,
            )

    @pytest.mark.asyncio
    async def test_collect_chunks_retries_empty_page_until_declared_total(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def list_chunks(self, dataset_id, document_id, page, page_size):
                self.calls += 1
                if self.calls == 1:
                    return {"data": {"chunks": [], "total": 2}}
                return {"data": {"chunks": [{"id": "c1"}, {"id": "c2"}], "total": 2}}

        collector = ParsingEvaluationCollector(EvaluationConfig())
        chunks = await collector._collect_chunks(
            FakeClient(),  # type: ignore[arg-type]
            "ds",
            "doc",
            timeout_seconds=30,
            poll_seconds=0,
        )
        assert len(chunks) == 2

    def test_env_dict_records_enterprise_commit_and_digests(self):
        env = _env_dict(
            "run-x",
            Path("enterprise/scripts/wp03/sample_manifest.json"),
            Path("enterprise/scripts/wp03/thresholds.json"),
            Path("artifacts/wp03/samples"),
            "tenant-x",
        )
        assert re.fullmatch(r"[0-9a-f]{40}", env["enterprise_commit"])
        assert isinstance(env["enterprise_worktree_dirty"], bool)
        assert env["manifest_digest"]
        assert env["thresholds_digest"]
        assert env["evaluation_contract_version"] == "1"

    def test_baseline_classification_rejects_dirty_unless_allowed(self):
        with pytest.raises(RuntimeError):
            _classify_baseline(True, False)
        assert _classify_baseline(True, True) == "informal_dirty_worktree"
        assert _classify_baseline(False, False) == "formal"
        assert (
            _classify_baseline(False, False, commit_unknown=True)
            == "informal_unknown_commit"
        )

    @pytest.mark.asyncio
    async def test_collection_failure_maps_to_failed(self):
        class FailingCollector:
            async def run_sample(self, sample, samples_dir, run_id):
                raise TimeoutError("boom")

        result = await _run_one_sample(
            FailingCollector(),  # type: ignore[arg-type]
            {"sample_id": "s1", "category": "digital_text", "pages": 2},
            Path("."),
            "run-1",
        )
        assert result["parse_quality_status"] == "failed"
        assert "TimeoutError" in result["quality_reasons"]
        assert result["metrics"]["parse_success"] is False

    def test_payload_external_document_id_includes_run_id(self, tmp_path):
        pdf = tmp_path / "s1.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake")
        collector = ParsingEvaluationCollector(EvaluationConfig())
        payload = collector._payload_for(
            {
                "sample_id": "wp03-digital_text-001",
                "file_name": "s1.pdf",
                "ground_truth_fields": {
                    "equipment_id": "EQ-1",
                    "fixed_asset_no": "FA-1",
                    "document_type": "manual",
                    "version": "V1",
                },
            },
            pdf,
            "run-x",
        )
        assert payload["externalDocumentId"] == "WP03-run-x-wp03-digital_text-001"

    def test_thresholds_preserve_metadata(self, tmp_path):
        path = tmp_path / "thresholds.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "threshold_version": 7,
                    "phase": "phase1_synthetic_baseline",
                    "temporary_conservative": True,
                    "min_key_field_accuracy": 1.0,
                }
            ),
            encoding="utf-8",
        )
        thresholds = load_thresholds(path)
        assert thresholds["threshold_version"] == 7
        assert thresholds["temporary_conservative"] is True
        assert thresholds["min_key_field_accuracy"] == 1.0
