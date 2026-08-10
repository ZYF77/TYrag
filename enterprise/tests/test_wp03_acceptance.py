"""WP-03 formal acceptance and unified runner regression tests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from enterprise.scripts.wp03.acceptance import (
    AcceptanceBlocked,
    PDF_PARSER_PROFILE,
    REQUIRED_ENV,
    SCENARIOS,
    SCENARIO_CATEGORIES,
    SCENARIO_EXPECTED_QUALITY,
    SCENARIO_MINIMUMS,
    SCENARIO_REQUIRED_CAPABILITIES,
    SCENARIO_REQUIRED_METRICS,
    _safe_url,
    _sample_failures,
    _verify_repeatability,
    preflight,
    run,
)


ROOT = Path(__file__).resolve().parents[2]


def _manifest(samples_dir: Path) -> dict:
    samples = []
    for scenario_id, dimensions in SCENARIOS.items():
        count = SCENARIO_MINIMUMS[scenario_id]
        for number in range(1, count + 1):
            file_name = f"{scenario_id}-{number}.pdf"
            content = f"sanitized-real-fixture-{scenario_id}-{number}".encode()
            (samples_dir / file_name).write_bytes(content)
            samples.append({
                "scenario_id": scenario_id,
                "sample_id": f"wp03-real-{scenario_id.lower()}-{number}",
                "category": SCENARIO_CATEGORIES[scenario_id],
                "file_name": file_name,
                "file_sha256": hashlib.sha256(content).hexdigest(),
                "pages": 3 if scenario_id == "S8" else 1,
                "parser_profile": PDF_PARSER_PROFILE,
                "ground_truth_fields": {
                    "equipment_id": f"EQ-{scenario_id}-{number}",
                    "document_type": "manual",
                    "version": "v1",
                },
                "expected_tables": [1] if scenario_id == "S2" else None,
                "expected_images": [1] if scenario_id in {"S3", "S4", "S5"} else None,
                "low_quality": scenario_id == "S7b",
                "expected_quality_status": SCENARIO_EXPECTED_QUALITY[scenario_id],
                "citation_questions": [
                    {
                        "question": f"Locate {scenario_id} fact {query}",
                        "expected_page": 1,
                        "expected_bbox": [0.1, 0.8, 0.2, 0.4],
                        "expected_answer_contains": [scenario_id],
                    }
                    for query in range(1, 5)
                ],
                "negative_questions": [
                    {"question": f"What unsupported fact is absent from {scenario_id}?"}
                ],
                "acceptance_dimensions": list(dimensions),
                "required_capabilities": list(
                    SCENARIO_REQUIRED_CAPABILITIES[scenario_id]
                ),
            })
    return {
        "schema_version": 1,
        "name": "sanitized real S1-S8 acceptance",
        "ground_truth_provenance": {
            "source": "sanitized_real_documents",
            "human_reviewed": True,
        },
        "samples": samples,
    }


def _write_manifest(tmp_path: Path) -> tuple[Path, Path]:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(samples_dir)), encoding="utf-8")
    return manifest_path, samples_dir


def _set_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.setenv(name, f"configured-{name.lower()}")
    monkeypatch.setenv("GATEWAY_URL", "http://user:secret@127.0.0.1:5188/path?token=x")
    monkeypatch.setenv("RAGFLOW_BASE_URL", "http://127.0.0.1:9380")
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:9000")


def test_preflight_requires_exact_real_s1_s8(tmp_path, monkeypatch):
    manifest_path, samples_dir = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed = manifest["samples"].pop()
    duplicate = dict(manifest["samples"][0])
    duplicate["sample_id"] = "replacement-s1"
    duplicate["file_name"] = removed["file_name"]
    duplicate["file_sha256"] = removed["file_sha256"]
    manifest["samples"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _set_live_env(monkeypatch)

    with pytest.raises(AcceptanceBlocked, match="scenario coverage is incomplete"):
        preflight(manifest_path, samples_dir)


def test_preflight_rejects_synthetic_or_unreviewed_ground_truth(tmp_path, monkeypatch):
    manifest_path, samples_dir = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ground_truth_provenance"] = {
        "source": "synthetic_generator",
        "human_reviewed": False,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _set_live_env(monkeypatch)

    with pytest.raises(AcceptanceBlocked, match="sanitized and human reviewed"):
        preflight(manifest_path, samples_dir)


def test_preflight_checks_fixture_hash_and_live_environment(tmp_path, monkeypatch):
    manifest_path, samples_dir = _write_manifest(tmp_path)
    _set_live_env(monkeypatch)
    manifest, evidence = preflight(manifest_path, samples_dir)
    assert len(manifest["samples"]) == 16
    assert {item["scenario_id"] for item in evidence} == set(SCENARIOS)
    by_scenario = {
        scenario_id: [
            sample for sample in manifest["samples"]
            if sample["scenario_id"] == scenario_id
        ]
        for scenario_id in SCENARIOS
    }
    assert by_scenario["S7a"][0]["expected_quality_status"] == "passed"
    assert by_scenario["S7b"][0]["expected_quality_status"] == "review_required"
    assert all(
        sample["parser_profile"] == PDF_PARSER_PROFILE
        and sample["required_capabilities"]
        for sample in manifest["samples"]
    )

    (samples_dir / "S4-1.pdf").write_bytes(b"tampered")
    with pytest.raises(AcceptanceBlocked, match="SHA-256 mismatch: S4"):
        preflight(manifest_path, samples_dir)


@pytest.mark.parametrize(
    ("scenario_id", "field", "value", "message"),
    [
        ("S7a", "category", "clear_scan", "S7a category must be degraded_scan"),
        ("S1", "parser_profile", "default", "S1 PDF parser_profile must be pdf_deepdoc_v1"),
        (
            "S2",
            "required_capabilities",
            ["text", "position", "citation"],
            "S2 required_capabilities must match",
        ),
        (
            "S7b",
            "expected_quality_status",
            "passed",
            "S7b expected_quality_status must be review_required",
        ),
    ],
)
def test_preflight_enforces_exact_scenario_contract(
    tmp_path,
    monkeypatch,
    scenario_id,
    field,
    value,
    message,
):
    manifest_path, samples_dir = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sample = next(
        item for item in manifest["samples"]
        if item["scenario_id"] == scenario_id
    )
    sample[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _set_live_env(monkeypatch)

    with pytest.raises(AcceptanceBlocked, match=message):
        preflight(manifest_path, samples_dir)


def test_missing_formal_corpus_returns_blocked_junit_and_evidence(tmp_path):
    args = argparse.Namespace(
        artifact_dir=str(tmp_path / "artifacts"),
        junit=str(tmp_path / "junit.xml"),
        run_id="blocked-run",
        manifest=str(tmp_path / "missing.json"),
        samples_dir=str(tmp_path / "missing-samples"),
        thresholds=str(ROOT / "enterprise/scripts/wp03/thresholds.json"),
        timeout=10,
    )

    assert run(args) == 2
    junit = Path(args.junit).read_text(encoding="utf-8")
    evidence = json.loads((Path(args.artifact_dir) / "evidence.json").read_text(encoding="utf-8"))
    assert 'tests="1"' in junit
    assert 'errors="1"' in junit
    assert 'skipped="0"' in junit
    assert evidence["status"] == "blocked"
    assert evidence["environment"]["postgres_integration"] == "not_applicable"


def test_safe_url_removes_credentials_query_and_fragment():
    assert _safe_url("https://user:secret@example.test:9443/api?token=x#part") == (
        "https://example.test:9443/api"
    )


def test_repeatability_requires_matching_fresh_parse_hashes(tmp_path):
    first = tmp_path / "first.json"
    repeat = tmp_path / "repeat.json"
    first.write_text(json.dumps({
        "summary": {
            "parse_repeatability_hash": "p",
            "e2e_repeatability_hash": "e",
        }
    }), encoding="utf-8")
    repeat.write_text(json.dumps({
        "summary": {
            "parse_repeatability_hash": "p",
            "e2e_repeatability_hash": "different",
        }
    }), encoding="utf-8")
    result = _verify_repeatability(first, repeat)
    assert result["status"] == "failed"
    assert "e2e_repeatability_hash" in result["detail"]


def test_formal_sample_hard_gates_cover_metrics_and_negative_refusal(tmp_path):
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    sample = _manifest(samples_dir)["samples"][0]
    metrics = {
        "parse_success": True,
        "out_of_range_page_count": 0,
        "page_coverage": 1.0,
        "position_coverage": 0.95,
        "key_field_accuracy": 1.0,
        "effective_text_coverage": 0.9,
        "required_capabilities": sample["required_capabilities"],
        "quality_expectations": {"declarations_complete": True},
    }
    metrics.update({
        "ocr_cer": 0.01,
        "citation_bbox_iou": 0.8,
        "retrieval_recall_at_8": 1.0,
        "no_answer_refusal_accuracy": 1.0,
        "citation_version_accuracy": 1.0,
    })
    assert set(SCENARIO_REQUIRED_METRICS["S1"]).issubset(metrics)
    document = {
        "parse_quality_status": "passed",
        "source_cleanup": "passed",
        "parser_application": {
            "state": "executed",
            "selectedProfile": PDF_PARSER_PROFILE,
            "configuredProfile": PDF_PARSER_PROFILE,
            "executedProfile": PDF_PARSER_PROFILE,
            "readbackMatch": True,
        },
        "metrics": metrics,
        "citation_results": [
            {
                "matched": True,
                "position_valid": True,
                "scope_document_match": True,
                "answer_matched": True,
                "version_match": True,
                "bbox_iou": 0.8,
            }
            for _ in sample["citation_questions"]
        ] + [{"expected_no_answer": True, "no_answer_refused": True}],
    }
    acl = {sample["sample_id"]: {"status": "passed"}}
    assert _sample_failures(sample, document, {}, acl) == []
    metrics["ocr_cer"] = 0.06
    assert any("ocr_cer" in failure for failure in _sample_failures(sample, document, {}, acl))


def test_unified_runner_profiles_and_strict_junit_contract():
    source = (ROOT / "enterprise/scripts/run_enterprise_tests.ps1").read_text(encoding="utf-8")
    assert "'Contract', 'P0', 'Integration', 'WP03', 'All'" in source
    assert "$counts.tests -eq 0" in source
    assert "$counts.skipped -gt 0" in source
    assert "Get-RagflowState" in source
    for code in range(5):
        assert f"{code} = {code}" in source


def test_phase2_b_uses_generated_degraded_scan_not_fake_pdf():
    source = (ROOT / "enterprise/scripts/wp03/phase2_e2e.py").read_text(encoding="utf-8")
    assert 'b_content = b_pdf.read_bytes()' in source
    assert '"B": await run_one(document_ids[1], "p2-b.pdf", b_content, 2)' in source
    assert "review required fixture for phase2 e2e" not in source
