"""Run the enterprise gateway for E2E with file-backed Uvicorn logging."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

LOG_PATH = ROOT / "artifacts" / "wp03-phase2-gateway-5190.log"

LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
    },
    "handlers": {
        "file": {
            "class": "logging.FileHandler",
            "filename": str(LOG_PATH),
            "formatter": "default",
        }
    },
    "loggers": {
        "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
}


if __name__ == "__main__":
    port = int(os.environ.get("GATEWAY_PORT", "5190"))
    uvicorn.run(
        "enterprise.gateway.app:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
        log_config=LOG_CONFIG,
    )
