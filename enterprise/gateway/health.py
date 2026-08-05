"""
Enterprise Gateway health probe.

Distinguishes liveness (process alive) from readiness (all external
dependencies reachable). Returns structured JSON suitable for
Kubernetes/Compose health checks.
"""

import json
import sys
from dataclasses import asdict

from enterprise.gateway.config import config
from enterprise.gateway.ragflow_client import RAGFlowClient


def check_health() -> dict:
    """Run all health probes and return a structured result."""
    client = RAGFlowClient()
    ragflow_status = client.health_check()

    return {
        "status": "ready" if ragflow_status.ready else "not_ready",
        "ragflow": asdict(ragflow_status),
        "config": {
            "ragflow_base_url": config.ragflow_base_url,
            "auth_enabled": config.auth_enabled,
        },
    }


def main():
    """CLI entrypoint for Docker healthcheck."""
    result = check_health()
    print(json.dumps(result, indent=2))
    if result["status"] != "ready":
        sys.exit(1)


if __name__ == "__main__":
    main()
