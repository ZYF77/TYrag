"""Safe backup manifest, restore, and RTO/RPO drill helper.

The command operates only on directories explicitly supplied by an operator.
It refuses symlinks, source/destination overlap, non-empty restore targets, and
destructive cleanup. The manifest contains hashes and relative paths; command
output contains counts and statuses only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


class BackupValidationError(ValueError):
    """A backup, restore target, or drill measurement is invalid."""


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupValidationError("timestamp is not RFC3339") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _assert_no_overlap(source: Path, destination: Path) -> None:
    source_resolved = _resolved(source)
    destination_resolved = _resolved(destination)
    if source_resolved == destination_resolved:
        raise BackupValidationError("source and destination must differ")
    if destination_resolved.is_relative_to(source_resolved) or source_resolved.is_relative_to(destination_resolved):
        raise BackupValidationError("source and destination must not overlap")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts or not relative:
        raise BackupValidationError("manifest path is unsafe")
    return relative


def _payload_files(root: Path, *, exclude_root_manifest: bool) -> list[Path]:
    if not root.exists() or not root.is_dir():
        raise BackupValidationError("backup root directory is unavailable")
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupValidationError("symlinks are not accepted in backup trees")
        if path.is_file():
            if exclude_root_manifest and path == root / MANIFEST_NAME:
                continue
            result.append(path)
    return result


def build_manifest(
    root: Path,
    *,
    captured_at: str | None = None,
    source_watermark: str | None = None,
) -> dict[str, Any]:
    """Build a hash manifest without copying or deleting any data."""
    root = _resolved(root)
    files = _payload_files(root, exclude_root_manifest=True)
    captured = _timestamp(captured_at)
    watermark = _timestamp(source_watermark)
    entries = [
        {
            "path": _relative_name(path, root),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": (captured or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "source_watermark": watermark.isoformat().replace("+00:00", "Z") if watermark else None,
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "files": entries,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupValidationError("manifest cannot be read") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise BackupValidationError("manifest schema is invalid")
    if not isinstance(payload.get("captured_at"), str):
        raise BackupValidationError("manifest capture time is invalid")
    _timestamp(payload["captured_at"])
    if payload.get("source_watermark") is not None:
        if not isinstance(payload["source_watermark"], str):
            raise BackupValidationError("manifest source watermark is invalid")
        _timestamp(payload["source_watermark"])
    files = payload.get("files")
    if (
        not isinstance(files, list)
        or payload.get("file_count") != len(files)
        or not isinstance(payload.get("total_bytes"), int)
        or payload["total_bytes"] < 0
    ):
        raise BackupValidationError("manifest file count is invalid")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BackupValidationError("manifest file entry is invalid")
        relative = item.get("path")
        if not isinstance(relative, str) or relative in seen:
            raise BackupValidationError("manifest contains duplicate or invalid paths")
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts or not relative:
            raise BackupValidationError("manifest contains unsafe path")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            raise BackupValidationError("manifest file size is invalid")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise BackupValidationError("manifest hash is invalid")
        seen.add(relative)
    return payload


def verify_manifest(root: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    """Verify hashes, sizes, missing files, and unexpected files."""
    root = _resolved(root)
    manifest = _read_manifest(_resolved(manifest_path or root / MANIFEST_NAME))
    expected = {item["path"]: item for item in manifest["files"]}
    missing = 0
    mismatched = 0
    for relative, item in expected.items():
        path = root / Path(*PurePosixPath(relative).parts)
        if not path.exists() or not path.is_file() or path.is_symlink():
            missing += 1
            continue
        if path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            mismatched += 1
    actual = {
        _relative_name(path, root)
        for path in _payload_files(root, exclude_root_manifest=True)
    }
    unexpected = len(actual - set(expected))
    declared_bytes = sum(item["size"] for item in manifest["files"])
    metadata_mismatch = int(declared_bytes != manifest["total_bytes"])
    passed = (
        missing == 0
        and mismatched == 0
        and unexpected == 0
        and metadata_mismatch == 0
    )
    return {
        "status": "passed" if passed else "failed",
        "file_count": len(expected),
        "total_bytes": manifest.get("total_bytes"),
        "missing_count": missing,
        "mismatch_count": mismatched,
        "unexpected_count": unexpected,
        "metadata_mismatch_count": metadata_mismatch,
    }


def _copy_tree(source: Path, destination: Path, *, manifest: dict[str, Any] | None = None) -> None:
    source = _resolved(source)
    destination = _resolved(destination)
    _assert_no_overlap(source, destination)
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise BackupValidationError("destination must be absent or empty")
    destination.mkdir(parents=True, exist_ok=True)
    files = manifest["files"] if manifest is not None else [
        {"path": _relative_name(path, source)} for path in _payload_files(source, exclude_root_manifest=False)
    ]
    for item in files:
        relative = item["path"]
        source_path = source / Path(*PurePosixPath(relative).parts)
        destination_path = destination / Path(*PurePosixPath(relative).parts)
        if not source_path.is_file() or source_path.is_symlink():
            raise BackupValidationError("source file is unavailable")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def create_backup(
    source_dir: Path,
    backup_dir: Path,
    *,
    source_watermark: str | None = None,
) -> dict[str, Any]:
    source = _resolved(source_dir)
    backup = _resolved(backup_dir)
    if (source / MANIFEST_NAME).exists():
        raise BackupValidationError("source root reserves manifest.json")
    _copy_tree(source, backup)
    manifest = build_manifest(backup, source_watermark=source_watermark)
    (backup / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = verify_manifest(backup)
    if result["status"] != "passed":
        raise BackupValidationError("backup integrity verification failed")
    return result


def restore_backup(backup_dir: Path, restore_dir: Path) -> dict[str, Any]:
    backup = _resolved(backup_dir)
    restore = _resolved(restore_dir)
    backup_result = verify_manifest(backup)
    if backup_result["status"] != "passed":
        raise BackupValidationError("backup failed integrity verification")
    manifest = _read_manifest(backup / MANIFEST_NAME)
    _copy_tree(backup, restore, manifest=manifest)
    result = verify_manifest(restore, manifest_path=backup / MANIFEST_NAME)
    if result["status"] != "passed":
        raise BackupValidationError("restored data failed integrity verification")
    return result


def calculate_rpo_seconds(latest_event_at: str, source_watermark: str) -> float:
    latest = _timestamp(latest_event_at)
    watermark = _timestamp(source_watermark)
    if latest is None or watermark is None:
        raise BackupValidationError("RPO timestamps are required")
    if watermark > latest:
        raise BackupValidationError("source watermark cannot be newer than latest event")
    return (latest - watermark).total_seconds()


def run_drill(
    source_dir: Path,
    backup_dir: Path,
    restore_dir: Path,
    *,
    source_watermark: str,
    latest_event_at: str,
    rto_target_seconds: float,
    rpo_target_seconds: float,
) -> dict[str, Any]:
    """Run a non-destructive copy/restore drill and evaluate RTO/RPO."""
    rto_target = float(rto_target_seconds)
    rpo_target = float(rpo_target_seconds)
    if rto_target < 0 or rpo_target < 0:
        raise BackupValidationError("RTO and RPO targets must be non-negative")
    backup_result = create_backup(source_dir, backup_dir, source_watermark=source_watermark)
    started = time.monotonic()
    restore_result = restore_backup(backup_dir, restore_dir)
    rto = time.monotonic() - started
    rpo = calculate_rpo_seconds(latest_event_at, source_watermark)
    checks = {
        "backup_integrity": backup_result["status"] == "passed",
        "restore_integrity": restore_result["status"] == "passed",
        "rto_target": rto <= rto_target,
        "rpo_target": rpo <= rpo_target,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "rto_seconds": round(rto, 6),
        "rpo_seconds": rpo,
        "rto_target_seconds": rto_target,
        "rpo_target_seconds": rpo_target,
        "file_count": restore_result["file_count"],
        "total_bytes": restore_result["total_bytes"],
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--source-dir", required=True, type=Path)
    create_parser.add_argument("--backup-dir", required=True, type=Path)
    create_parser.add_argument("--source-watermark")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--backup-dir", required=True, type=Path)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup-dir", required=True, type=Path)
    restore_parser.add_argument("--restore-dir", required=True, type=Path)

    drill_parser = subparsers.add_parser("drill")
    drill_parser.add_argument("--source-dir", required=True, type=Path)
    drill_parser.add_argument("--backup-dir", required=True, type=Path)
    drill_parser.add_argument("--restore-dir", required=True, type=Path)
    drill_parser.add_argument("--source-watermark", required=True)
    drill_parser.add_argument("--latest-event-at", required=True)
    drill_parser.add_argument("--rto-target-seconds", required=True, type=float)
    drill_parser.add_argument("--rpo-target-seconds", required=True, type=float)

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            _emit({"status": "passed", "operation": "create", "result": create_backup(args.source_dir, args.backup_dir, source_watermark=args.source_watermark)})
        elif args.command == "verify":
            result = verify_manifest(args.backup_dir)
            _emit({"status": result["status"], "operation": "verify", "result": result})
            return 0 if result["status"] == "passed" else 1
        elif args.command == "restore":
            _emit({"status": "passed", "operation": "restore", "result": restore_backup(args.backup_dir, args.restore_dir)})
        else:
            result = run_drill(
                args.source_dir,
                args.backup_dir,
                args.restore_dir,
                source_watermark=args.source_watermark,
                latest_event_at=args.latest_event_at,
                rto_target_seconds=args.rto_target_seconds,
                rpo_target_seconds=args.rpo_target_seconds,
            )
            _emit(result)
            return 0 if result["status"] == "passed" else 1
        return 0
    except (BackupValidationError, OSError):
        _emit({"status": "failed", "reason": "backup or restore validation failed"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
