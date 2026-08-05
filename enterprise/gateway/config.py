"""
Enterprise Gateway configuration.

All settings are loaded from environment variables with sensible defaults.
No real credentials, keys, or secrets belong in this file.
"""

import os
from dataclasses import dataclass, field


@dataclass
class GatewayConfig:
    """Central configuration for the enterprise integration gateway."""

    # --- RAGFlow connection ---
    ragflow_base_url: str = field(
        default_factory=lambda: os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
    )
    ragflow_timeout: float = field(
        default_factory=lambda: float(os.getenv("RAGFLOW_TIMEOUT", "30.0"))
    )
    ragflow_api_version: str = field(
        default_factory=lambda: os.getenv("RAGFLOW_API_VERSION", "v1")
    )

    # --- Business PostgreSQL (future) ---
    pg_host: str = field(
        default_factory=lambda: os.getenv("PG_HOST", "localhost")
    )
    pg_port: int = field(
        default_factory=lambda: int(os.getenv("PG_PORT", "5432"))
    )
    pg_database: str = field(
        default_factory=lambda: os.getenv("PG_DATABASE", "")
    )
    pg_user: str = field(
        default_factory=lambda: os.getenv("PG_USER", "")
    )
    pg_password: str = field(
        default_factory=lambda: os.getenv("PG_PASSWORD", "")
    )
    pg_timeout: float = field(
        default_factory=lambda: float(os.getenv("PG_TIMEOUT", "10.0"))
    )

    # --- Object storage (future) ---
    s3_endpoint: str = field(
        default_factory=lambda: os.getenv("S3_ENDPOINT", "")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.getenv("S3_BUCKET", "")
    )

    # --- Logging & observability ---
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    # --- Feature flags ---
    auth_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTH_ENABLED", "true").lower() == "true"
    )

    @property
    def ragflow_api_url(self) -> str:
        return f"{self.ragflow_base_url}/api/{self.ragflow_api_version}"


# Singleton instance
config = GatewayConfig()
