"""Unit tests for GatewayConfig."""

import os
import pytest
from enterprise.gateway.config import (
    DEFAULT_DOCUMENT_FEED_MAX_SIZE_MB,
    GatewayConfig,
    require_ragflow_api_key,
)


class TestGatewayConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", raising=False)
        cfg = GatewayConfig()
        assert cfg.ragflow_base_url == "http://localhost:9380"
        assert cfg.ragflow_timeout == 120.0
        assert cfg.ragflow_api_version == "v1"
        assert cfg.auth_enabled is True
        assert cfg.transient_attachments_enabled is True

    def test_ragflow_api_url_property(self):
        cfg = GatewayConfig()
        assert cfg.ragflow_api_url == "http://localhost:9380/api/v1"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RAGFLOW_BASE_URL", "http://ragflow:9380")
        monkeypatch.setenv("RAGFLOW_TIMEOUT", "60.0")
        monkeypatch.setenv("AUTH_ENABLED", "false")
        cfg = GatewayConfig()
        assert cfg.ragflow_base_url == "http://ragflow:9380"
        assert cfg.ragflow_timeout == 60.0
        assert cfg.auth_enabled is False

    def test_document_feed_limit_never_exceeds_ragflow_ceiling(self, monkeypatch):
        monkeypatch.setenv("S3_MAX_SIZE_MB", "512")
        monkeypatch.setenv("ENTERPRISE_FILE_SHARE_MAX_SIZE_MB", "512")
        cfg = GatewayConfig()
        assert cfg.s3_max_size_mb == DEFAULT_DOCUMENT_FEED_MAX_SIZE_MB
        assert cfg.file_share_max_size_mb == DEFAULT_DOCUMENT_FEED_MAX_SIZE_MB

    def test_ragflow_processing_mirrors_are_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("RAGFLOW_MAX_CONCURRENT_TASKS", "3")
        monkeypatch.setenv("RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS", "2")
        monkeypatch.setenv("RAGFLOW_EXECUTOR_WORKERS", "1")
        cfg = GatewayConfig()
        assert cfg.ragflow_max_concurrent_tasks == 3
        assert cfg.ragflow_max_concurrent_chunk_builders == 2
        assert cfg.ragflow_executor_workers == 1

    def test_ragflow_processing_mirrors_accept_ragflow_source_names(self, monkeypatch):
        monkeypatch.delenv("RAGFLOW_MAX_CONCURRENT_TASKS", raising=False)
        monkeypatch.delenv("RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS", raising=False)
        monkeypatch.delenv("RAGFLOW_EXECUTOR_WORKERS", raising=False)
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "3")
        monkeypatch.setenv("MAX_CONCURRENT_CHUNK_BUILDERS", "2")
        monkeypatch.setenv("WORKERS", "1")
        cfg = GatewayConfig()
        assert cfg.ragflow_max_concurrent_tasks == 3
        assert cfg.ragflow_max_concurrent_chunk_builders == 2
        assert cfg.ragflow_executor_workers == 1

    def test_transient_attachments_are_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", raising=False)
        assert GatewayConfig().transient_attachments_enabled is True

        monkeypatch.setenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", "false")
        assert GatewayConfig().transient_attachments_enabled is False

    def test_context_compress_defaults_and_override(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_CONTEXT_COMPRESS_ENABLED", raising=False)
        monkeypatch.delenv("ENTERPRISE_CONTEXT_COMPRESS_TURNS", raising=False)
        monkeypatch.delenv("ENTERPRISE_CONTEXT_SUMMARY_MAX_CHARS", raising=False)
        cfg = GatewayConfig()
        assert cfg.context_compress_enabled is True
        assert cfg.context_compress_turns == 20
        assert cfg.context_summary_max_chars == 1500

        monkeypatch.setenv("ENTERPRISE_CONTEXT_COMPRESS_ENABLED", "false")
        monkeypatch.setenv("ENTERPRISE_CONTEXT_COMPRESS_TURNS", "8")
        overridden = GatewayConfig()
        assert overridden.context_compress_enabled is False
        assert overridden.context_compress_turns == 8

    def test_rag_diagnostics_defaults_off_and_can_be_enabled(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_RAG_DIAGNOSTICS_ENABLED", raising=False)
        assert GatewayConfig().rag_diagnostics_enabled is False

        monkeypatch.setenv("ENTERPRISE_RAG_DIAGNOSTICS_ENABLED", "true")
        assert GatewayConfig().rag_diagnostics_enabled is True

    def test_pg_defaults(self):
        cfg = GatewayConfig()
        assert cfg.pg_host == "localhost"
        assert cfg.pg_port == 5432
        assert cfg.pg_timeout == 10.0

    def test_no_hardcoded_secrets(self):
        """Ensure no real credentials in default values."""
        cfg = GatewayConfig()
        assert cfg.pg_password == ""
        assert "infini" not in cfg.ragflow_base_url.lower()

    def test_demo_routes_default_disabled_outside_test_mode(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
        monkeypatch.delenv("ENTERPRISE_DEMO_ROUTES_ENABLED", raising=False)
        assert GatewayConfig().demo_routes_enabled is False

    def test_demo_routes_enabled_in_test_mode(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
        monkeypatch.delenv("ENTERPRISE_DEMO_ROUTES_ENABLED", raising=False)
        assert GatewayConfig().demo_routes_enabled is True

    def test_demo_routes_explicit_override(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_DEMO_ROUTES_ENABLED", "true")
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
        assert GatewayConfig().demo_routes_enabled is True

    def test_ragflow_api_key_required_outside_test_mode(self, monkeypatch):
        monkeypatch.delenv("RAGFLOW_API_KEY", raising=False)
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
        with pytest.raises(RuntimeError):
            require_ragflow_api_key()

    def test_ragflow_api_key_allowed_in_test_mode(self, monkeypatch):
        monkeypatch.delenv("RAGFLOW_API_KEY", raising=False)
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "1")
        assert require_ragflow_api_key() == "stub-key"

    def test_ragflow_api_key_returns_configured_value(self, monkeypatch):
        monkeypatch.setenv("RAGFLOW_API_KEY", "ragflow-key")
        monkeypatch.setenv("ENTERPRISE_TEST_MODE", "0")
        assert require_ragflow_api_key() == "ragflow-key"
