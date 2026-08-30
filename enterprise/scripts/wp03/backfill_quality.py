"""Safe backfill CLI that creates quality evaluations for existing ready docs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.db.dialect import fetchall  # noqa: E402
from enterprise.gateway.db.ops import gw_read, gw_write  # noqa: E402
from enterprise.gateway.quality.models import (  # noqa: E402
    get_latest_evaluation,
    get_or_create_evaluation,
)
from enterprise.gateway.quality.routing import route_document  # noqa: E402
from enterprise.gateway.db.testing import create_gateway  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    gateway = await create_gateway(args.db)
    try:
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
        async with gateway.transaction(write=False) as conn:
            rows = await fetchall(conn, query, params)

        created = 0
        skipped = 0
        for row in rows:
            latest = await gw_read(
                gateway,
                get_latest_evaluation,
                row["tenant_id"],
                row["source_system"],
                row["external_document_id"],
                row["source_version_id"],
            )
            if latest is not None:
                skipped += 1
                continue
            if args.dry_run:
                created += 1
                continue
            routing = route_document(
                media_type=row.get("media_type"),
                file_name=row.get("file_name"),
                source_system=row["source_system"],
            )
            await gw_write(
                gateway,
                get_or_create_evaluation,
                row["tenant_id"],
                row["source_system"],
                row["external_document_id"],
                row["source_version_id"],
                row.get("ragflow_dataset_id"),
                row.get("ragflow_document_id"),
                routing,
                max_attempts=args.max_attempts,
            )
            created += 1
        print(f"created={created} skipped={skipped} dry_run={args.dry_run}")
        return 0
    finally:
        await gateway.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--tenant")
    parser.add_argument("--source-system")
    parser.add_argument("--source-version-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
