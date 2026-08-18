"""
Enterprise Gateway configuration.

All settings are loaded from environment variables with sensible defaults.
No real credentials, keys, or secrets belong in this file.
"""

import json
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
        default_factory=lambda: float(os.getenv("RAGFLOW_TIMEOUT", "120.0"))
    )
    ragflow_api_version: str = field(
        default_factory=lambda: os.getenv("RAGFLOW_API_VERSION", "v1")
    )

    # --- Business PostgreSQL read-only adapter ---
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
    business_query_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_BUSINESS_QUERY_ENABLED", "false"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    business_query_transport: str = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_BUSINESS_QUERY_TRANSPORT", "unconfigured"
        ).strip().lower()
    )
    business_query_max_rows: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_BUSINESS_QUERY_MAX_ROWS", "100")
        )
    )
    business_query_max_range_days: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_BUSINESS_QUERY_MAX_RANGE_DAYS", "31")
        )
    )

    # --- Business TimeSeries read-only adapter ---
    timeseries_query_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_TIMESERIES_QUERY_ENABLED", "false"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    timeseries_query_transport: str = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_TIMESERIES_QUERY_TRANSPORT", "unconfigured"
        ).strip().lower()
    )
    timeseries_timeout: float = field(
        default_factory=lambda: float(
            os.getenv("ENTERPRISE_TIMESERIES_TIMEOUT", "10.0")
        )
    )
    timeseries_query_max_range_hours: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_TIMESERIES_MAX_RANGE_HOURS", "24")
        )
    )

    # --- Object storage (future) ---
    s3_endpoint: str = field(
        default_factory=lambda: os.getenv("S3_ENDPOINT", "")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.getenv("S3_BUCKET", "")
    )
    s3_access_key: str = field(
        default_factory=lambda: os.getenv("S3_ACCESS_KEY", "")
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.getenv("S3_SECRET_KEY", "")
    )
    s3_region: str = field(
        default_factory=lambda: os.getenv("S3_REGION", "")
    )
    s3_path_style: bool = field(
        default_factory=lambda: os.getenv("S3_PATH_STYLE", "true").lower() == "true"
    )
    s3_max_size_mb: int = field(
        default_factory=lambda: int(os.getenv("S3_MAX_SIZE_MB", "512"))
    )

    # --- WP-02 outbox / worker ---
    outbox_max_attempts: int = field(
        default_factory=lambda: int(os.getenv("ENTERPRISE_OUTBOX_MAX_ATTEMPTS", "5"))
    )
    outbox_poll_seconds: float = field(
        default_factory=lambda: float(os.getenv("ENTERPRISE_OUTBOX_POLL_SECONDS", "2.0"))
    )
    reconcile_seconds: float = field(
        default_factory=lambda: float(os.getenv("ENTERPRISE_RECONCILE_SECONDS", "10.0"))
    )
    worker_enabled: bool = field(
        default_factory=lambda: os.getenv("ENTERPRISE_WORKER_ENABLED", "true").lower() == "true"
    )

    # --- WP-03 Phase 2: quality evaluation ---
    quality_worker_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_QUALITY_WORKER_ENABLED", "true"
        ).lower() == "true"
    )
    quality_poll_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("ENTERPRISE_QUALITY_POLL_SECONDS", "2.0")
        )
    )
    quality_reconcile_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("ENTERPRISE_QUALITY_RECONCILE_SECONDS", "10.0")
        )
    )
    quality_max_attempts: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_QUALITY_MAX_ATTEMPTS", "5")
        )
    )
    quality_strict_mode: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_QUALITY_STRICT_MODE", "true"
        ).lower() == "true"
    )
    quality_demo_warn_mode: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_QUALITY_DEMO_WARN_MODE", "false"
        ).lower() == "true"
    )
    quality_running_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_QUALITY_RUNNING_TIMEOUT_SECONDS", "1800")
        )
    )

    # --- FILE_SHARE outbound terminal callbacks ---
    callback_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_CALLBACK_ENABLED", "false"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    callback_hmac_secret: str = field(
        default_factory=lambda: os.getenv("ENTERPRISE_CALLBACK_HMAC_SECRET", "")
    )
    callback_max_attempts: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_CALLBACK_MAX_ATTEMPTS", "8")
        )
    )
    callback_poll_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("ENTERPRISE_CALLBACK_POLL_SECONDS", "2.0")
        )
    )

    @property
    def demo_routes_enabled(self) -> bool:
        default = (
            "1"
            if os.getenv("ENTERPRISE_TEST_MODE") == "1"
            else "0"
        )
        return os.getenv(
            "ENTERPRISE_DEMO_ROUTES_ENABLED", default
        ).lower() in ("1", "true", "yes", "on")

    def validate_business_query(self) -> None:
        """Validate safe adapter settings without inspecting or logging secrets.

        ``external`` means that the application will inject a transport
        implementation.  A driver name is deliberately not accepted until a
        customer schema and dependency contract are frozen.
        """
        transport_values = {"unconfigured", "external"}
        if self.business_query_transport not in transport_values:
            raise ValueError("unsupported business query transport")
        if self.timeseries_query_transport not in transport_values:
            raise ValueError("unsupported time-series query transport")
        if not 1 <= self.pg_port <= 65535:
            raise ValueError("PG_PORT must be between 1 and 65535")
        if not self.business_query_max_rows or not (
            1 <= self.business_query_max_rows <= 500
        ):
            raise ValueError("business query row limit is outside the safe range")
        if not 1 <= self.business_query_max_range_days <= 365:
            raise ValueError("business query range is outside the safe range")
        if not 1 <= self.timeseries_query_max_range_hours <= 168:
            raise ValueError("time-series query range is outside the safe range")
        for value, name in (
            (self.pg_timeout, "PG_TIMEOUT"),
            (self.timeseries_timeout, "ENTERPRISE_TIMESERIES_TIMEOUT"),
        ):
            if (
                value != value
                or value in (float("inf"), float("-inf"))
                or not 0 < value <= 60
            ):
                raise ValueError(f"{name} must be between 0 and 60 seconds")
        if self.business_query_enabled:
            if self.business_query_transport == "unconfigured":
                raise ValueError(
                    "business query transport must be injected when enabled"
                )
            if not self.pg_database.strip() or not self.pg_user.strip():
                raise ValueError(
                    "PG_DATABASE and PG_USER are required when business query is enabled"
                )
        if self.timeseries_query_enabled and (
            self.timeseries_query_transport == "unconfigured"
        ):
            raise ValueError(
                "time-series query transport must be injected when enabled"
            )

    # --- Logging & observability ---
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    # --- Feature flags ---
    auth_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTH_ENABLED", "true").lower() == "true"
    )
    transient_attachments_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED", "true"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    context_compress_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ENTERPRISE_CONTEXT_COMPRESS_ENABLED", "true"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    context_compress_turns: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_CONTEXT_COMPRESS_TURNS", "20")
        )
    )
    context_summary_max_chars: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_CONTEXT_SUMMARY_MAX_CHARS", "1500")
        )
    )
    context_compress_keep_recent: int = field(
        default_factory=lambda: int(
            os.getenv("ENTERPRISE_CONTEXT_COMPRESS_KEEP_RECENT", "4")
        )
    )

    # --- JWT / User Auth (WP-01A) ---
    jwt_issuer: str = field(
        default_factory=lambda: os.getenv("JWT_ISSUER", "")
    )
    jwt_audience: str = field(
        default_factory=lambda: os.getenv("JWT_AUDIENCE", "")
    )
    jwt_jwks_url: str = field(
        default_factory=lambda: os.getenv("JWT_JWKS_URL", "")
    )
    jwt_allowed_algs: str = field(
        default_factory=lambda: os.getenv("JWT_ALLOWED_ALGS", "RS256,ES256")
    )
    jwt_claim_map: dict = field(
        default_factory=lambda: json.loads(os.getenv("JWT_CLAIM_MAP",
            '{"sub":"sub","tenant_id":"tenant","business_user_id":"business_user_id",'
            '"display_name":"name","department_ids":"department",'
            '"role_codes":"roles","group_ids":"groups","security_level":"security_level"}'))
    )
    jwt_enable_hs: bool = field(
        default_factory=lambda: os.getenv("JWT_ENABLE_HS", "").lower() == "true"
    )

    @property
    def ragflow_api_url(self) -> str:
        return f"{self.ragflow_base_url}/api/{self.ragflow_api_version}"


# Singleton instance
config = GatewayConfig()


def require_ragflow_api_key() -> str:
    """Return the configured RAGFlow API key or fail fast outside test mode.

    A silently empty key would make every RAGFlow call fail later with an
    opaque 401, so non-test startup and request paths must reject it early.
    """
    key = os.getenv("RAGFLOW_API_KEY", "").strip()
    if not key and os.getenv("ENTERPRISE_TEST_MODE") != "1":
        raise RuntimeError(
            "RAGFLOW_API_KEY is required when ENTERPRISE_TEST_MODE != 1"
        )
    return key or "stub-key"
