"""Tests for enterprise health probe."""

from enterprise.gateway.health import check_health
from enterprise.gateway.ragflow_client import RAGFlowStub


class TestHealthCheck:
    def test_ready_when_ragflow_healthy(self, monkeypatch):
        def _fake_client():
            return RAGFlowStub(healthy=True)
        monkeypatch.setattr(
            "enterprise.gateway.health.RAGFlowClient", _fake_client
        )
        result = check_health()
        assert result["status"] == "ready"
        assert result["ragflow"]["live"] is True

    def test_not_ready_when_ragflow_unhealthy(self, monkeypatch):
        def _fake_client():
            return RAGFlowStub(healthy=False)
        monkeypatch.setattr(
            "enterprise.gateway.health.RAGFlowClient", _fake_client
        )
        result = check_health()
        assert result["status"] == "not_ready"
        assert result["ragflow"]["live"] is False
