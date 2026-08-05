"""Unit tests for GatewayConfig."""

import os
import pytest
from enterprise.gateway.config import GatewayConfig


class TestGatewayConfig:
    def test_defaults(self):
        cfg = GatewayConfig()
        assert cfg.ragflow_base_url == "http://localhost:9380"
        assert cfg.ragflow_timeout == 30.0
        assert cfg.ragflow_api_version == "v1"
        assert cfg.auth_enabled is True

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
