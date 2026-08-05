"""Shared test fixtures for enterprise layer."""

import pytest
from enterprise.gateway.ragflow_client import RAGFlowStub


@pytest.fixture
def ragflow_stub():
    """Return a RAGFlowStub that simulates a healthy RAGFlow."""
    return RAGFlowStub(healthy=True)


@pytest.fixture
def ragflow_stub_unhealthy():
    """Return a RAGFlowStub that simulates an unhealthy RAGFlow."""
    return RAGFlowStub(healthy=False)
