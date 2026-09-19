"""RAGFlowAgentClient.timeout must accept parent __init__ assignment."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from enterprise.gateway.query.workflow_client import RAGFlowAgentClient


def test_ragflow_agent_client_timeout_setter_is_noop_and_getter_reads_runtime():
    """Parent sets ``self.timeout``; property setter must not AttributeError."""
    runtime = SimpleNamespace(workflow_timeout_seconds=177.0)

    with patch(
        "enterprise.gateway.query.workflow_client.require_ragflow_api_key",
        return_value="test-key",
    ), patch(
        "enterprise.gateway.query.workflow_client.config"
    ) as cfg:
        cfg.ragflow_base_url = "http://ragflow.test"
        cfg.ragflow_timeout = 33.0
        cfg.workflow_timeout = 120.0
        cfg.runtime_settings.return_value = runtime

        client = RAGFlowAgentClient()
        # Construction succeeded (parent wrote timeout through no-op setter).
        assert client.timeout == 177.0

        # Explicit assignment must not raise and must not shadow the getter.
        client.timeout = 9.0
        assert client.timeout == 177.0

        runtime.workflow_timeout_seconds = 88.5
        assert client.timeout == 88.5
