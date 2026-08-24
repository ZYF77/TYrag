"""Safe read-only access to host-mounted FILE_SHARE sources."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from enterprise.gateway.sync.source_adapter import (
    SourceFetchError,
    SourceHashMismatch,
    SourceTooLarge,
)


class FileShareConfigurationError(SourceFetchError):
    """The configured read-only root cannot be used safely."""


@dataclass(frozen=True)
class SourceStat:
    size: int
    modified_ns: int
    etag: str


def _max_size_bytes() -> int:
    try:
        return max(1, int(os.getenv("ENTERPRISE_FILE_SHARE_MAX_SIZE_MB", "512"))) * 1024 * 1024
    except ValueError:
        return 512 * 1024 * 1024


def _configured_roots() -> dict[str, Path]:
    """Load deployment-provided read-only mount roots."""

    raw = os.getenv("ENTERPRISE_FILE_SHARE_ROOTS", "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FileShareConfigurationError("Invalid FILE_SHARE root registry") from exc
        if not isinstance(value, dict):
            raise FileShareConfigurationError("FILE_SHARE root registry must be an object")
        roots = {
            str(root_id): Path(str(root_path))
            for root_id, root_path in value.items()
            if str(root_id).strip() and str(root_path).strip()
        }
        if roots:
            return roots
    root = os.getenv("ENTERPRISE_FILE_SHARE_ROOT", "").strip()
    root_id = os.getenv("ENTERPRISE_FILE_SHARE_ROOT_ID", "default").strip() or "default"
    return {root_id: Path(root)} if root else {}


def _safe_relative_path(relative_path: str) -> Path:
    normalized = str(relative_path or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ":" in normalized
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FileShareConfigurationError("relativePath must stay below the configured root")
    return Path(*pure.parts)


class FileShareSourceAdapter:
    """Safely opens a file below a configured read-only FILE_SHARE root."""

    def __init__(
        self,
        roots: dict[str, str | Path] | None = None,
        max_size_bytes: int | None = None,
    ) -> None:
        self._roots = {
            str(root_id): Path(root_path)
            for root_id, root_path in (roots or {}).items()
        }
        self.max_size_bytes = max_size_bytes or _max_size_bytes()

    def _root_registry(self) -> dict[str, Path]:
        return self._roots or _configured_roots()

    @staticmethod
    def _source_stat(stat: os.stat_result) -> SourceStat:
        return SourceStat(
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            etag=f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"',
        )

    def resolve_path(self, storage_root_id: str, relative_path: str) -> Path:
        roots = self._root_registry()
        root = roots.get(storage_root_id)
        if root is None:
            raise FileShareConfigurationError("Unknown FILE_SHARE storage root")
        safe_relative = _safe_relative_path(relative_path)
        root_resolved = root.resolve(strict=False)
        candidate = (root_resolved / safe_relative).resolve(strict=False)
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise FileShareConfigurationError("FILE_SHARE path escapes configured root") from exc
        if not candidate.is_file():
            raise SourceFetchError("FILE_SHARE source file was not found")
        return candidate

    def stat_source(self, storage_root_id: str, relative_path: str) -> SourceStat:
        path = self.resolve_path(storage_root_id, relative_path)
        current = self._source_stat(path.stat())
        if current.size > self.max_size_bytes:
            raise SourceTooLarge(f"FILE_SHARE source exceeds {self.max_size_bytes} bytes")
        return current

    def open_verified(
        self,
        storage_root_id: str,
        relative_path: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
        expected_etag: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> BinaryIO:
        """Return the verified handle rewound for the multipart upload."""

        path = self.resolve_path(storage_root_id, relative_path)
        handle = path.open("rb")
        try:
            current = self._source_stat(os.fstat(handle.fileno()))
            if current.size > self.max_size_bytes:
                raise SourceTooLarge(
                    f"FILE_SHARE source exceeds {self.max_size_bytes} bytes"
                )
            if expected_size is not None and int(expected_size) != current.size:
                raise SourceFetchError("FILE_SHARE source size changed")
            if expected_etag and expected_etag != current.etag:
                raise SourceFetchError("FILE_SHARE source etag changed")

            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
            if digest.hexdigest().lower() != expected_sha256.lower():
                raise SourceHashMismatch(
                    "FILE_SHARE source SHA256 does not match the version contract"
                )
            handle.seek(0)
            return handle
        except Exception:
            handle.close()
            raise
