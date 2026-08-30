#!/usr/bin/env python3
"""One-time, fail-closed migration from Gateway SQLite to PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.gateway.db.database import GatewayDatabase, resolve_database_url, schema_for_key
from enterprise.gateway.db.tables import ALL_TABLES


def _safe_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def _update_digest(digest: Any, values: Iterable[Any]) -> None:
    payload = json.dumps(
        [_safe_value(value) for value in values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest.update(payload)
    digest.update(b"\n")


def _source_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _source_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


async def _target_must_be_empty(conn: Any) -> None:
    nonempty: list[str] = []
    for table in ALL_TABLES:
        count = int((await conn.execute(select(func.count()).select_from(table))).scalar_one())
        if count:
            nonempty.append(f"{table.name}={count}")
    if nonempty:
        raise RuntimeError("PostgreSQL target is not empty: " + ", ".join(nonempty))


async def _copy_table(
    source: sqlite3.Connection,
    target: Any,
    table: Any,
    *,
    present: bool,
    batch_size: int,
) -> dict[str, Any]:
    if not present:
        return {"sourcePresent": False, "rows": 0, "sha256": hashlib.sha256().hexdigest()}

    source_columns = _source_columns(source, table.name)
    target_columns = [column.name for column in table.columns]
    extra = sorted(set(source_columns) - set(target_columns))
    if extra:
        raise RuntimeError(f"{table.name} has unmapped SQLite columns: {extra!r}")
    copied_columns = [name for name in target_columns if name in source_columns]
    missing_required = [
        column.name
        for column in table.columns
        if column.name not in source_columns
        and not column.nullable
        and column.default is None
        and column.server_default is None
        and not (column.primary_key and column.autoincrement)
    ]
    source_count = int(
        source.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
    )
    if source_count and missing_required:
        raise RuntimeError(
            f"{table.name} is missing required columns: {missing_required!r}"
        )

    primary_keys = [column.name for column in table.primary_key.columns]
    order_columns = primary_keys or copied_columns
    projection = ", ".join(f'"{name}"' for name in copied_columns)
    ordering = ", ".join(f'"{name}"' for name in order_columns)
    cursor = source.execute(
        f'SELECT {projection} FROM "{table.name}" ORDER BY {ordering}'
    )
    source_digest = hashlib.sha256()
    inserted = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        payload = []
        for row in rows:
            values = [row[name] for name in copied_columns]
            _update_digest(source_digest, values)
            payload.append(dict(zip(copied_columns, values, strict=True)))
        await target.execute(table.insert(), payload)
        inserted += len(payload)

    selected = select(*(table.c[name] for name in copied_columns))
    if order_columns:
        selected = selected.order_by(*(table.c[name] for name in order_columns))
    target_digest = hashlib.sha256()
    target_count = 0
    result = await target.stream(selected)
    async for row in result:
        _update_digest(target_digest, row)
        target_count += 1
    if inserted != source_count or target_count != source_count:
        raise RuntimeError(
            f"{table.name} row count mismatch: source={source_count}, "
            f"inserted={inserted}, target={target_count}"
        )
    if target_digest.hexdigest() != source_digest.hexdigest():
        raise RuntimeError(f"{table.name} digest mismatch")

    if "id" in table.c and table.c.id.primary_key:
        await target.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name}"
            )
        )
    return {
        "sourcePresent": True,
        "rows": source_count,
        "sha256": source_digest.hexdigest(),
        "columns": copied_columns,
    }


async def migrate(
    sqlite_path: Path,
    *,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    source = _source_connection(sqlite_path)
    gateway = GatewayDatabase(resolve_database_url(), schema=schema_for_key())
    try:
        await gateway.initialize()
        conn = await gateway.connect()
        transaction = await conn.begin()
        try:
            await _target_must_be_empty(conn)
            source_tables = _source_table_names(source)
            tables: dict[str, Any] = {}
            for table in ALL_TABLES:
                tables[table.name] = await _copy_table(
                    source,
                    conn,
                    table,
                    present=table.name in source_tables,
                    batch_size=max(1, batch_size),
                )
            manifest = {
                "format": "tyrag-gateway-sqlite-to-postgresql-v1",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "dryRun": dry_run,
                "tables": tables,
                "totalRows": sum(item["rows"] for item in tables.values()),
            }
            if dry_run:
                await transaction.rollback()
            else:
                await transaction.commit()
            return manifest
        except BaseException:
            await transaction.rollback()
            raise
        finally:
            await conn.close()
    finally:
        source.close()
        await gateway.dispose()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    manifest = asyncio.run(
        migrate(
            args.sqlite_path,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    )
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "verified" if args.dry_run else "migrated",
                "totalRows": manifest["totalRows"],
                "manifest": str(args.manifest),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
