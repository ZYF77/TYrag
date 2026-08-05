"""Verify version-manifest.json is valid and complete."""

import json
import os
import pytest


@pytest.fixture
def manifest():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "version-manifest.json"
    )
    with open(path) as f:
        return json.load(f)


class TestVersionManifest:
    def test_is_valid_json(self, manifest):
        assert "project" in manifest
        assert "ragflow_upstream" in manifest

    def test_source_tag(self, manifest):
        assert manifest["ragflow_upstream"]["source_tag"].startswith("v")

    def test_source_commit_is_hex(self, manifest):
        commit = manifest["ragflow_upstream"]["source_commit"]
        assert len(commit) == 40
        assert all(c in "0123456789abcdef" for c in commit)

    def test_docker_image_digest(self, manifest):
        digest = manifest["ragflow_upstream"]["docker_image_digest"]
        assert digest.startswith("sha256:")
        assert len(digest) > len("sha256:")

    def test_doc_engine_known(self, manifest):
        assert manifest["ragflow_upstream"]["doc_engine"] in (
            "elasticsearch",
            "infinity",
            "opensearch",
            "oceanbase",
        )

    def test_has_entrypoint_field(self, manifest):
        assert "entrypoint" in manifest["ragflow_upstream"]

    def test_has_compose_file_field(self, manifest):
        assert "compose_file" in manifest["ragflow_upstream"]

    def test_has_migration_baseline(self, manifest):
        assert "database_migration_baseline" in manifest["ragflow_upstream"]

    def test_handoff_spec_version(self, manifest):
        assert "handoff_spec_version" in manifest
        assert manifest["handoff_spec_version"] == "1.0"

    def test_contracts_listed(self, manifest):
        contracts = manifest["contracts"]
        assert "integration_openapi" in contracts
        assert "metadata_schema" in contracts
        assert "error_codes" in contracts

    def test_no_unresolved_markers(self, manifest):
        """No field should contain 'unresolved' unless explicitly allowed."""
        upstream = manifest["ragflow_upstream"]
        for key, value in upstream.items():
            if isinstance(value, str):
                assert "unresolved" not in value.lower(), (
                    f"Field {key} is unresolved: {value}"
                )
