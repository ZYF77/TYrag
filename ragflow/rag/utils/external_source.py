"""One-shot Gateway source-ticket reader for virtual external documents."""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


def _max_size_bytes() -> int:
    try:
        return max(1, int(os.getenv("TYRAG_EXTERNAL_SOURCE_MAX_SIZE_MB", "512"))) * 1024 * 1024
    except ValueError:
        return 512 * 1024 * 1024


def fetch_external_source(location: str) -> bytes:
    """Fetch ``external://<ticket>`` into a short-lived temp file and verify it."""

    if not isinstance(location, str) or not location.startswith("external://"):
        raise ValueError("invalid external source location")
    ticket = location[len("external://") :]
    if not ticket or len(ticket) > 256:
        raise ValueError("invalid external source ticket")
    base_url = (
        os.getenv("TYRAG_EXTERNAL_SOURCE_GATEWAY_URL")
        or os.getenv("ENTERPRISE_EXTERNAL_SOURCE_GATEWAY_URL")
        or "http://enterprise-gateway:9380"
    ).rstrip("/")
    url = f"{base_url}/enterprise/internal/source-tickets/{quote(ticket, safe='')}"
    headers = {"Accept": "application/pdf"}
    internal_key = os.getenv("TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY", "")
    if internal_key:
        headers["X-Internal-Source-Key"] = internal_key

    digest = hashlib.sha256()
    total = 0
    max_size = _max_size_bytes()
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            declared_length = response.headers.get("Content-Length")
            declared_size = None
            if declared_length:
                try:
                    declared_size = int(declared_length)
                except ValueError as exc:
                    raise ValueError("external source returned an invalid size") from exc
            if declared_size is not None and declared_size > max_size:
                raise ValueError("external source exceeds configured size limit")
            with tempfile.TemporaryDirectory(prefix="tyrag-source-") as temp_dir:
                path = Path(temp_dir) / "source.pdf"
                with path.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_size:
                            raise ValueError("external source exceeds configured size limit")
                        digest.update(chunk)
                        output.write(chunk)
                if declared_size is not None and declared_size != total:
                    raise ValueError("external source size verification failed")
                expected_sha256 = (response.headers.get("X-Source-SHA256") or "").lower()
                actual_sha256 = digest.hexdigest()
                if not expected_sha256 or expected_sha256 != actual_sha256:
                    raise ValueError("external source hash verification failed")
                return path.read_bytes()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"external source gateway returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("external source gateway is unavailable") from exc
