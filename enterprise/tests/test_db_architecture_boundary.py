"""Architecture boundary: Gateway runtime is PostgreSQL-only."""

from __future__ import annotations

import ast
from pathlib import Path

def _imports_sqlite(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in {"aiosqlite", "sqlite3"} for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.module in {"aiosqlite", "sqlite3"}:
            return True
    return False


def test_gateway_runtime_has_no_sqlite_imports_or_sql():
    root = Path(__file__).resolve().parents[2] / "enterprise" / "gateway"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if _imports_sqlite(path) or any(
            marker in source
            for marker in ("PRAGMA ", "BEGIN IMMEDIATE", "sqlite_master", "AUTOINCREMENT")
        ):
            offenders.append(str(path.relative_to(root.parents[1])))
    assert not offenders, f"SQLite runtime dependency remains: {offenders}"
