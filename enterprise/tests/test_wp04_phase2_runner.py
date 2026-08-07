"""WP-04 Phase 2 runner sample-preparation orchestration tests."""
import hashlib
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.scripts.run_wp04_phase2_e2e import (  # noqa: E402
    WP03_SAMPLE_IDS,
    prepare_wp03_samples,
)


def _write_sample(sample_dir: Path, sample_id: str) -> Path:
    path = sample_dir / f"{sample_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"sample")
    return path


def test_samples_existing_skips_generator(tmp_path):
    for sample_id in WP03_SAMPLE_IDS:
        _write_sample(tmp_path, sample_id)

    called: list[int] = []
    info = prepare_wp03_samples(
        tmp_path,
        generator=lambda: called.append(1) or 0,
    )
    assert info["mode"] == "existing"
    assert called == []
    assert info["sha256"][WP03_SAMPLE_IDS[0]] == hashlib.sha256(
        b"sample"
    ).hexdigest()


def test_missing_samples_generate_and_continue(tmp_path):
    generated: list[Path] = []

    def generator() -> int:
        generated.extend(
            _write_sample(tmp_path, sample_id)
            for sample_id in WP03_SAMPLE_IDS
        )
        return 0

    info = prepare_wp03_samples(tmp_path, generator=generator)
    assert info["mode"] == "generated"
    assert len(generated) == len(WP03_SAMPLE_IDS)
    assert all(path.exists() for path in generated)


def test_generator_failure_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="exit code 1"):
        prepare_wp03_samples(tmp_path, generator=lambda: 1)


def test_generator_success_without_files_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="did not create required PDFs"):
        prepare_wp03_samples(tmp_path, generator=lambda: 0)
