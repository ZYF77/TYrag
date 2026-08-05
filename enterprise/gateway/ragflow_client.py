"""
Minimal RAGFlow API client/adapter.

Only implements the skeleton needed for WP-00 baseline:
  - health check
  - API version/capability probe
  - timeout and error handling
  - requestId propagation

Does NOT implement file sync, chat, session, or ACL logic.
Uses only RAGFlow public REST API; never accesses internal databases.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from enterprise.gateway.config import config

logger = logging.getLogger(__name__)


class RAGFlowError(Exception):
    """Base error for RAGFlow client failures."""

    def __init__(self, message: str, request_id: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.request_id = request_id
        self.status_code = status_code


class RAGFlowTimeoutError(RAGFlowError):
    """Timeout while waiting for RAGFlow."""


class RAGFlowConnectionError(RAGFlowError):
    """Cannot reach RAGFlow at all."""


@dataclass
class HealthStatus:
    """Result of a RAGFlow health check."""

    live: bool
    ready: bool = False
    version: str = ""
    doc_engine: str = ""
    request_id: str | None = None
    error: str | None = None


class RAGFlowClient:
    """
    Anti-corruption layer around RAGFlow HTTP API.

    All requests go through this client so that upstream API changes
    are isolated to a single module. A test stub (RAGFlowStub) can
    replace this for offline testing.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = base_url or config.ragflow_api_url
        self.timeout = timeout or config.ragflow_timeout

    def _new_request_id(self) -> str:
        return str(uuid.uuid4())

    def _build_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def health_check(self) -> HealthStatus:
        """
        Check whether RAGFlow is live.

        Returns HealthStatus with live=True only if the ping endpoint responds.
        This is a basic liveness check, not a full readiness probe.
        """
        request_id = self._new_request_id()
        try:
            import urllib.request

            url = f"{config.ragflow_base_url}/api/v1/system/ping"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            body = resp.read().decode()

            if body.strip() == "pong":
                return HealthStatus(live=True, ready=True, request_id=request_id)
            return HealthStatus(live=True, ready=False, request_id=request_id, error=f"unexpected response: {body}")
        except Exception as e:
            logger.warning("RAGFlow health check failed: %s", e)
            return HealthStatus(live=False, request_id=request_id, error=str(e))

    def get_version(self) -> dict[str, Any]:
        """
        Probe RAGFlow version and capabilities.

        Returns a dict with version info. Raises RAGFlowError on failure.
        """
        request_id = self._new_request_id()
        try:
            import json
            import urllib.request

            url = f"{config.ragflow_base_url}/api/v1/system/version"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            return json.loads(resp.read().decode())
        except Exception as e:
            raise RAGFlowError(f"Failed to get version: {e}", request_id=request_id) from e


class RAGFlowStub(RAGFlowClient):
    """
    Test stub that returns canned responses without network calls.

    Use in unit tests to avoid Docker dependency.
    """

    def __init__(self, healthy: bool = True, version: dict | None = None):
        super().__init__(base_url="stub://test", timeout=1.0)
        self._healthy = healthy
        self._version = version or {"version": "stub-0.0.0"}

    def health_check(self) -> HealthStatus:
        if self._healthy:
            return HealthStatus(live=True, ready=True, request_id="stub-request-id")
        return HealthStatus(live=False, request_id="stub-request-id", error="stub: simulated failure")

    def get_version(self) -> dict[str, Any]:
        if not self._healthy:
            raise RAGFlowError("stub: RAGFlow unavailable", request_id="stub-request-id")
        return self._version
