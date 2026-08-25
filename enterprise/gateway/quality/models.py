"""Enterprise-side parse quality evaluation persistence and job queue."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from enterprise.gateway.quality.metrics import metrics

EVALUATION_CONTRACT = "contracts/parse-quality-evaluation.md"
EVALUATION_CONTRACT_VERSION = "1"

CREATE_QUALITY_EVALUATION = """
CREATE TABLE IF NOT EXISTS parse_quality_evaluation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    ragflow_dataset_id TEXT,
    ragflow_document_id TEXT,
    evaluation_version TEXT NOT NULL DEFAULT '1',
    evaluation_contract_version TEXT NOT NULL DEFAULT '1',
    thresholds_version TEXT,
    thresholds_digest TEXT,
    parser_profile TEXT,
    parser_version TEXT,
    routing_policy_version TEXT,
    routing_reasons TEXT NOT NULL DEFAULT '[]',
    evaluation_state TEXT NOT NULL DEFAULT 'pending',
    parse_quality_status TEXT,
    quality_reasons TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    parse_repeatability_hash TEXT,
    e2e_repeatability_hash TEXT,
    artifact_hash TEXT,
    enterprise_commit TEXT,
    enterprise_worktree_dirty INTEGER NOT NULL DEFAULT 0,
    ragflow_source_tag TEXT,
    ragflow_source_commit TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, source_system, external_document_id,
           source_version_id, evaluation_version)
);

CREATE INDEX IF NOT EXISTS idx_quality_eval_doc
    ON parse_quality_evaluation(
        tenant_id, source_system, external_document_id, source_version_id
    );

CREATE INDEX IF NOT EXISTS idx_quality_eval_state
    ON parse_quality_evaluation(evaluation_state, parse_quality_status);
"""

CREATE_QUALITY_JOB = """
CREATE TABLE IF NOT EXISTS quality_evaluation_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    evaluation_version TEXT NOT NULL DEFAULT '1',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at TEXT,
    locked_at TEXT,
    worker_id TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_job_pending
    ON quality_evaluation_job(status, next_retry_at);
"""


@dataclass
class QualityEvaluation:
    id: int
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    ragflow_dataset_id: str | None
    ragflow_document_id: str | None
    evaluation_version: str
    evaluation_contract_version: str
    thresholds_version: str | None
    thresholds_digest: str | None
    parser_profile: str | None
    parser_version: str | None
    routing_policy_version: str | None
    routing_reasons: list[str] = field(default_factory=list)
    evaluation_state: str = "pending"
    parse_quality_status: str | None = None
    quality_reasons: list[str] = field(default_factory=list)
    metrics_json: dict[str, Any] = field(default_factory=dict)
    parse_repeatability_hash: str | None = None
    e2e_repeatability_hash: str | None = None
    artifact_hash: str | None = None
    enterprise_commit: str | None = None
    enterprise_worktree_dirty: bool = False
    ragflow_source_tag: str | None = None
    ragflow_source_commit: str | None = None
    attempt_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "source_system": self.source_system,
            "external_document_id": self.external_document_id,
            "source_version_id": self.source_version_id,
            "ragflow_dataset_id": self.ragflow_dataset_id,
            "ragflow_document_id": self.ragflow_document_id,
            "evaluation_version": self.evaluation_version,
            "evaluation_contract_version": self.evaluation_contract_version,
            "thresholds_version": self.thresholds_version,
            "thresholds_digest": self.thresholds_digest,
            "parser_profile": self.parser_profile,
            "parser_version": self.parser_version,
            "routing_policy_version": self.routing_policy_version,
            "routing_reasons": self.routing_reasons,
            "evaluation_state": self.evaluation_state,
            "parse_quality_status": self.parse_quality_status,
            "quality_reasons": self.quality_reasons,
            "metrics_json": self.metrics_json,
            "parse_repeatability_hash": self.parse_repeatability_hash,
            "e2e_repeatability_hash": self.e2e_repeatability_hash,
            "artifact_hash": self.artifact_hash,
            "enterprise_commit": self.enterprise_commit,
            "enterprise_worktree_dirty": self.enterprise_worktree_dirty,
            "ragflow_source_tag": self.ragflow_source_tag,
            "ragflow_source_commit": self.ragflow_source_commit,
            "attempt_count": self.attempt_count,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class QualityJob:
    id: int
    evaluation_id: int
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    evaluation_version: str
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: str | None = None
    locked_at: str | None = None
    worker_id: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row_to_evaluation(row: aiosqlite.Row) -> QualityEvaluation:
    return QualityEvaluation(
        id=row["id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        ragflow_dataset_id=row["ragflow_dataset_id"],
        ragflow_document_id=row["ragflow_document_id"],
        evaluation_version=row["evaluation_version"],
        evaluation_contract_version=row["evaluation_contract_version"],
        thresholds_version=row["thresholds_version"],
        thresholds_digest=row["thresholds_digest"],
        parser_profile=row["parser_profile"],
        parser_version=row["parser_version"],
        routing_policy_version=row["routing_policy_version"],
        routing_reasons=_json_loads(row["routing_reasons"], []),
        evaluation_state=row["evaluation_state"],
        parse_quality_status=row["parse_quality_status"],
        quality_reasons=_json_loads(row["quality_reasons"], []),
        metrics_json=_json_loads(row["metrics_json"], {}),
        parse_repeatability_hash=row["parse_repeatability_hash"],
        e2e_repeatability_hash=row["e2e_repeatability_hash"],
        artifact_hash=row["artifact_hash"],
        enterprise_commit=row["enterprise_commit"],
        enterprise_worktree_dirty=bool(row["enterprise_worktree_dirty"]),
        ragflow_source_tag=row["ragflow_source_tag"],
        ragflow_source_commit=row["ragflow_source_commit"],
        attempt_count=row["attempt_count"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_job(row: aiosqlite.Row) -> QualityJob:
    return QualityJob(
        id=row["id"],
        evaluation_id=row["evaluation_id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        evaluation_version=row["evaluation_version"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        next_retry_at=row["next_retry_at"],
        locked_at=row["locked_at"],
        worker_id=row["worker_id"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_job(row: aiosqlite.Row) -> QualityJob:
    """Public row converter used by worker/reconciler code."""
    return _row_to_job(row)


async def ensure_quality_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(CREATE_QUALITY_EVALUATION)
    await db.executescript(CREATE_QUALITY_JOB)
    await db.commit()


async def get_evaluation_by_id(
    db: aiosqlite.Connection, evaluation_id: int,
) -> QualityEvaluation | None:
    async with db.execute(
        "SELECT * FROM parse_quality_evaluation WHERE id=?",
        (evaluation_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_evaluation(row) if row else None


async def get_evaluation(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    evaluation_version: str,
) -> QualityEvaluation | None:
    async with db.execute(
        """SELECT * FROM parse_quality_evaluation
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
             AND source_version_id=? AND evaluation_version=?""",
        (
            tenant_id, source_system, external_document_id,
            source_version_id, evaluation_version,
        ),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_evaluation(row) if row else None


async def get_latest_evaluation(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str | None = None,
) -> QualityEvaluation | None:
    params: list[object] = [tenant_id, source_system, external_document_id]
    version_clause = ""
    if source_version_id:
        version_clause = "AND source_version_id=?"
        params.append(source_version_id)
    async with db.execute(
        f"""SELECT * FROM parse_quality_evaluation
            WHERE tenant_id=? AND source_system=? AND external_document_id=?
            {version_clause}
            ORDER BY id DESC LIMIT 1""",
        params,
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_evaluation(row) if row else None


async def list_evaluations(
    db: aiosqlite.Connection,
    tenant_id: str | None = None,
    source_system: str | None = None,
    status: str | None = None,
    parser_profile: str | None = None,
    batch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QualityEvaluation]:
    clauses: list[str] = []
    params: list[object] = []
    if tenant_id:
        clauses.append("e.tenant_id=?")
        params.append(tenant_id)
    if source_system:
        clauses.append("e.source_system=?")
        params.append(source_system)
    if status:
        clauses.append("e.parse_quality_status=?")
        params.append(status)
    if parser_profile:
        clauses.append("e.parser_profile=?")
        params.append(parser_profile)
    if batch_id:
        clauses.append("m.batch_id=?")
        params.append(batch_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    async with db.execute(
        f"""SELECT e.* FROM parse_quality_evaluation e
            LEFT JOIN ext_document_map m
              ON m.tenant_id=e.tenant_id
             AND m.source_system=e.source_system
             AND m.external_document_id=e.external_document_id
             AND m.source_version_id=e.source_version_id
            {where}
            ORDER BY e.id DESC LIMIT ? OFFSET ?""",
        params,
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_evaluation(r) for r in rows]


async def get_or_create_evaluation(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
    ragflow_dataset_id: str | None,
    ragflow_document_id: str | None,
    routing: dict[str, Any] | None = None,
    evaluation_version: str = "1",
    max_attempts: int = 5,
) -> QualityEvaluation:
    routing = routing or {}
    existing = await get_evaluation(
        db, tenant_id, source_system, external_document_id,
        source_version_id, evaluation_version,
    )
    if existing:
        job = await get_job_by_evaluation_id(db, existing.id)
        if job is None and existing.evaluation_state == "pending":
            now = utc_now()
            await db.execute(
                """INSERT INTO quality_evaluation_job
                   (evaluation_id, tenant_id, source_system,
                    external_document_id, source_version_id,
                    evaluation_version, status, attempts, max_attempts,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
                (
                    existing.id, tenant_id, source_system,
                    external_document_id, source_version_id,
                    evaluation_version, max_attempts, now, now,
                ),
            )
            await db.commit()
        return existing

    now = utc_now()
    try:
        cursor = await db.execute(
            """INSERT INTO parse_quality_evaluation
               (tenant_id, source_system, external_document_id,
                source_version_id, ragflow_dataset_id, ragflow_document_id,
                evaluation_version, evaluation_contract_version,
                parser_profile, parser_version, routing_policy_version,
                routing_reasons, evaluation_state, metrics_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?, ?)
               ON CONFLICT(tenant_id, source_system, external_document_id,
                           source_version_id, evaluation_version)
               DO NOTHING""",
            (
                tenant_id, source_system, external_document_id,
                source_version_id, ragflow_dataset_id, ragflow_document_id,
                evaluation_version, EVALUATION_CONTRACT_VERSION,
                routing.get("selected_parser_profile"),
                routing.get("parser_version"),
                routing.get("routing_policy_version"),
                _json_dumps(routing.get("routing_reasons") or []),
                now, now,
            ),
        )
        await db.commit()
        if cursor.rowcount:
            evaluation_id = cursor.lastrowid
            metrics.inc("quality_evaluation_pending_total")
            await db.execute(
                """INSERT INTO quality_evaluation_job
                   (evaluation_id, tenant_id, source_system,
                    external_document_id, source_version_id,
                    evaluation_version, status, attempts, max_attempts,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
                (
                    evaluation_id, tenant_id, source_system,
                    external_document_id, source_version_id,
                    evaluation_version, max_attempts, now, now,
                ),
            )
            await db.commit()
            return await get_evaluation_by_id(db, evaluation_id)
    except sqlite3.IntegrityError:
        await db.rollback()
    return await get_evaluation(
        db, tenant_id, source_system, external_document_id,
        source_version_id, evaluation_version,
    ) or existing


async def next_evaluation_version(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> str:
    latest = await get_latest_evaluation(
        db, tenant_id, source_system, external_document_id, source_version_id,
    )
    if latest is None:
        return "1"
    try:
        return str(int(latest.evaluation_version) + 1)
    except (TypeError, ValueError):
        return "2"


async def start_evaluation(db: aiosqlite.Connection, evaluation_id: int) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE parse_quality_evaluation
           SET evaluation_state='running', started_at=COALESCE(started_at, ?),
               updated_at=?
           WHERE id=?""",
        (now, now, evaluation_id),
    )
    await db.commit()


async def complete_evaluation(
    db: aiosqlite.Connection,
    evaluation_id: int,
    *,
    parse_quality_status: str,
    quality_reasons: list[str],
    metrics_json: dict[str, Any],
    parse_repeatability_hash: str | None,
    e2e_repeatability_hash: str | None,
    artifact_hash: str | None,
    enterprise_commit: str | None,
    enterprise_worktree_dirty: bool,
    ragflow_source_tag: str | None,
    ragflow_source_commit: str | None,
    thresholds_version: str | None,
    thresholds_digest: str | None,
) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE parse_quality_evaluation
           SET evaluation_state='completed',
               parse_quality_status=?,
               quality_reasons=?,
               metrics_json=?,
               parse_repeatability_hash=?,
               e2e_repeatability_hash=?,
               artifact_hash=?,
               enterprise_commit=?,
               enterprise_worktree_dirty=?,
               ragflow_source_tag=?,
               ragflow_source_commit=?,
               thresholds_version=?,
               thresholds_digest=?,
               completed_at=?,
               last_error_code=NULL,
               last_error_message=NULL,
               updated_at=?
           WHERE id=?""",
        (
            parse_quality_status, _json_dumps(quality_reasons),
            _json_dumps(metrics_json), parse_repeatability_hash,
            e2e_repeatability_hash, artifact_hash, enterprise_commit,
            1 if enterprise_worktree_dirty else 0,
            ragflow_source_tag, ragflow_source_commit,
            thresholds_version, thresholds_digest, now, now, evaluation_id,
        ),
    )
    await db.commit()


async def fail_evaluation(
    db: aiosqlite.Connection,
    evaluation_id: int,
    error_code: str,
    error_message: str,
    *,
    parse_quality_status: str | None = None,
) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE parse_quality_evaluation
           SET evaluation_state='failed',
               parse_quality_status=?,
               last_error_code=?,
               last_error_message=?,
               updated_at=?
           WHERE id=?""",
        (parse_quality_status, error_code, error_message, now, evaluation_id),
    )
    await db.commit()


async def claim_quality_job(
    db: aiosqlite.Connection, worker_id: str, limit: int = 1,
) -> list[QualityJob]:
    now = utc_now()
    cursor = await db.execute(
        """UPDATE quality_evaluation_job
           SET status='running', locked_at=?, worker_id=?,
               attempts=attempts+1, updated_at=?
           WHERE id IN (
               SELECT id FROM quality_evaluation_job
               WHERE status='pending'
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY id LIMIT ?
           )""",
        (now, worker_id, now, now, limit),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return []
    async with db.execute(
        """SELECT * FROM quality_evaluation_job
           WHERE worker_id=? AND locked_at=? AND status='running'
           ORDER BY id LIMIT ?""",
        (worker_id, now, limit),
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]


async def get_job_by_evaluation_id(
    db: aiosqlite.Connection, evaluation_id: int,
) -> QualityJob | None:
    async with db.execute(
        "SELECT * FROM quality_evaluation_job WHERE evaluation_id=?",
        (evaluation_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_job(row) if row else None


async def mark_quality_job_done(
    db: aiosqlite.Connection, job: QualityJob,
) -> None:
    await db.execute(
        """UPDATE quality_evaluation_job
           SET status='done', locked_at=NULL, worker_id=NULL,
               next_retry_at=NULL, last_error_code=NULL,
               last_error_message=NULL, updated_at=?
           WHERE id=?""",
        (utc_now(), job.id),
    )
    await db.commit()
    job.status = "done"


async def mark_quality_job_retry(
    db: aiosqlite.Connection,
    job: QualityJob,
    error_code: str,
    error_message: str,
) -> None:
    delay_seconds = min(2 ** max(job.attempts - 1, 0), 60)
    next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat()
    await db.execute(
        """UPDATE quality_evaluation_job
           SET status='pending', locked_at=NULL, worker_id=NULL,
               next_retry_at=?, last_error_code=?, last_error_message=?,
               updated_at=?
           WHERE id=?""",
        (next_retry_at, error_code, error_message, utc_now(), job.id),
    )
    await db.commit()
    job.status = "pending"
    job.next_retry_at = next_retry_at
    job.last_error_code = error_code
    job.last_error_message = error_message


async def mark_quality_job_failed(
    db: aiosqlite.Connection,
    job: QualityJob,
    error_code: str,
    error_message: str,
) -> None:
    status = "dead" if job.attempts >= job.max_attempts else "failed"
    await db.execute(
        """UPDATE quality_evaluation_job
           SET status=?, locked_at=NULL, worker_id=NULL,
               last_error_code=?, last_error_message=?, updated_at=?
           WHERE id=?""",
        (status, error_code, error_message, utc_now(), job.id),
    )
    await db.commit()
    job.status = status
    job.last_error_code = error_code
    job.last_error_message = error_message
