"""Safe backfill CLI that creates quality evaluations for existing ready docs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.quality.models import (  # noqa: E402
    get_latest_evaluation,
    get_or_create_evaluation,
)
from enterprise.gateway.quality.routing import route_document  # noqa: E402
from enterprise.gateway.sync.models import init_db  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    db = await init_db(args.db)
    clauses = [
        "sync_status='ready'",
        "business_status='active'",
        "current_version=1",
    ]
    params: list[object] = []
    if args.tenant:
        clauses.append("tenant_id=?")
        params.append(args.tenant)
    if args.source_system:
        clauses.append("source_system=?")
        params.append(args.source_system)
    if args.source_version_id:
        clauses.append("source_version_id=?")
        params.append(args.source_version_id)
    params.extend([args.limit, args.offset])
    query = (
        f"SELECT * FROM ext_document_map WHERE {' AND '.join(clauses)} "
        "ORDER BY id LIMIT ? OFFSET ?"
    )
    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    created = 0
    skipped = 0
    for row in rows:
        latest = await get_latest_evaluation(
            db,
            row["tenant_id"],
            row["source_system"],
            row["external_document_id"],
            row["source_version_id"],
        )
        if latest is not None:
            skipped += 1
            continue
        if args.dry_run:
            print(
                f"[dry-run] would create {row['external_document_id']} "
                f"version={row['source_version_id']}"
            )
            created += 1
            continue
        routing = route_document(
            media_type=row["media_type"],
            file_name=row["file_name"],
            source_system=row["source_system"],
        )
        await get_or_create_evaluation(
            db,
            tenant_id=row["tenant_id"],
            source_system=row["source_system"],
            external_document_id=row["external_document_id"],
            source_version_id=row["source_version_id"],
            ragflow_dataset_id=row["ragflow_dataset_id"],
            ragflow_document_id=row["ragflow_document_id"],
            routing=routing,
            evaluation_version="1",
            max_attempts=args.max_attempts,
        )
        created += 1

    print(
        f"backfill {'dry-run ' if args.dry_run else ''}"
        f"created={created} skipped_existing={skipped} scanned={len(rows)}"
    )
    await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill quality evaluations for existing ready documents"
    )
    parser.add_argument("--db", default="enterprise/ext_document_map.db")
    parser.add_argument("--tenant")
    parser.add_argument("--source-system")
    parser.add_argument("--source-version-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
