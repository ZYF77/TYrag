"""Unit tests for GatewayConfig."""

import os
import pytest
from enterprise.gateway.config import GatewayConfig, require_ragflow_api_key


class TestGatewayConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", raising=False)
        cfg = GatewayConfig()
        assert cfg.ragflow_base_url == "http://localhost:9380"
        assert cfg.ragflow_timeout == 30.0
        assert cfg.ragflow_api_version == "v1"
        assert cfg.auth_enabled is True
        assert cfg.transient_attachments_enabled is False

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

    def test_transient_attachments_require_explicit_enablement(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", raising=False)
        assert GatewayConfig().transient_attachments_enabled is False

        monkeypatch.setenv("ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", "true")
        assert GatewayConfig().transient_attachments_enabled is True

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
