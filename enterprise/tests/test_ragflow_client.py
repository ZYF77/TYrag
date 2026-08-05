"""Unit tests for RAGFlowClient using stub."""

import pytest
from enterprise.gateway.ragflow_client import (
    RAGFlowClient,
    RAGFlowError,
    RAGFlowStub,
    HealthStatus,
)


class TestRAGFlowStub:
    def test_stub_healthy(self, ragflow_stub):
        status = ragflow_stub.health_check()
        assert status.live is True
        assert status.ready is True
        assert status.request_id is not None

    def test_stub_unhealthy(self, ragflow_stub_unhealthy):
        status = ragflow_stub_unhealthy.health_check()
        assert status.live is False
        assert status.error is not None

    def test_stub_version(self, ragflow_stub):
        v = ragflow_stub.get_version()
        assert "version" in v

    def test_stub_version_unhealthy_raises(self, ragflow_stub_unhealthy):
        with pytest.raises(RAGFlowError):
            ragflow_stub_unhealthy.get_version()


class TestHealthStatus:
    def test_default(self):
        s = HealthStatus(live=False)
        assert s.ready is False

    def test_live_and_ready(self):
        s = HealthStatus(live=True, ready=True, version="v0.26.4", doc_engine="elasticsearch")
        assert s.live
        assert s.ready
        assert s.version == "v0.26.4"


class TestRAGFlowClientTimeout:
    def test_timeout_default(self):
        client = RAGFlowClient()
        assert client.timeout == 30.0

    def test_timeout_custom(self):
        client = RAGFlowClient(timeout=5.0)
        assert client.timeout == 5.0
