"""One-shot migration helper: transform aiosqlite store patterns to SQLAlchemy AsyncConnection."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPLACEMENTS = [
    (r"import aiosqlite\n", ""),
    (r"import sqlite3\n", ""),
    (r"from typing import Any\n", "from typing import Any, Mapping\n"),
    (
        r"from enterprise\.gateway\.db\.dialect import exec_sql, fetchall, fetchone\n",
        "from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone\n",
    ),
    (r"aiosqlite\.Connection", "AsyncConnection"),
    (r"aiosqlite\.Row", "Mapping[str, Any]"),
    (r"db: AsyncConnection", "conn: AsyncConnection"),
    (r"\bdb\b(?=\s*:\s*AsyncConnection)", "conn"),
    (r"async def (\w+)\(\s*conn:", r"async def \1(conn:"),
]

PATTERN_EXECUTE = re.compile(
    r"async with (\w+)\.execute\(\s*(\"\"\"[\s\S]*?\"\"\"|'[^']*'|\([^)]+\)),\s*(\([^)]*\)|\w+)\s*\) as cursor:\s*\n"
    r"(?:\s*(\w+) = await cursor\.fetchone\(\)|\s*(\w+) = await cursor\.fetchall\(\))",
    re.MULTILINE,
)


def migrate_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "AsyncConnection" not in text and "aiosqlite" not in text:
        return
    if "from sqlalchemy.ext.asyncio import AsyncConnection" not in text:
        if "from typing import" in text:
            text = text.replace(
                "from typing import",
                "from sqlalchemy.ext.asyncio import AsyncConnection\nfrom enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone\nfrom typing import",
                1,
            )
        else:
            text = (
                "from sqlalchemy.ext.asyncio import AsyncConnection\n"
                "from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone\n"
                + text
            )
    for old, new in REPLACEMENTS:
        text = re.sub(old, new, text)
    path.write_text(text, encoding="utf-8")
    print(f"migrated {path}")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        migrate_file(Path(arg))
