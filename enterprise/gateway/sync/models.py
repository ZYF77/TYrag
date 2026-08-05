"""Enterprise sync database models, outbox, and schema migration."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite


CREATE_EXT_DOCUMENT_MAP = """
CREATE TABLE IF NOT EXISTS ext_document_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'upsert',
    event_status TEXT NOT NULL DEFAULT 'received',
    sha256 TEXT NOT NULL,
    file_name TEXT NOT NULL,
    media_type TEXT DEFAULT 'application/pdf',
    bucket TEXT NOT NULL DEFAULT '',
    object_key TEXT NOT NULL DEFAULT '',
    ragflow_dataset_id TEXT,
    ragflow_document_id TEXT,
    ragflow_task_id TEXT,
    sync_status TEXT NOT NULL DEFAULT 'received',
    pipeline_status TEXT,
    business_status TEXT NOT NULL DEFAULT 'active',
    current_version INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    batch_id TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    last_sync_at TEXT,
    source_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(tenant_id, source_system, external_document_id, source_version_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ext_doc_event
    ON ext_document_map(event_id);

CREATE INDEX IF NOT EXISTS idx_ext_doc_status
    ON ext_document_map(tenant_id, sync_status);

CREATE INDEX IF NOT EXISTS idx_ext_doc_doc
    ON ext_document_map(tenant_id, source_system, external_document_id);

CREATE INDEX IF NOT EXISTS idx_ext_doc_sha
    ON ext_document_map(tenant_id, ragflow_dataset_id, sha256);

CREATE INDEX IF NOT EXISTS idx_ext_doc_batch
    ON ext_document_map(tenant_id, batch_id);
"""


CREATE_SYNC_OUTBOX = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    batch_id TEXT,
    payload TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON sync_outbox(status, next_retry_at);
"""


_MIGRATION_COLUMNS = {
    "event_type": "TEXT NOT NULL DEFAULT 'upsert'",
    "event_status": "TEXT NOT NULL DEFAULT 'received'",
    "bucket": "TEXT NOT NULL DEFAULT ''",
    "object_key": "TEXT NOT NULL DEFAULT ''",
    "business_status": "TEXT NOT NULL DEFAULT 'active'",
    "current_version": "INTEGER NOT NULL DEFAULT 0",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "next_retry_at": "TEXT",
    "batch_id": "TEXT",
}


@dataclass
class ExtDocumentMap:
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    event_id: str
    sha256: str
    file_name: str
    media_type: str = "application/pdf"
    event_type: str = "upsert"
    event_status: str = "received"
    bucket: str = ""
    object_key: str = ""
    ragflow_dataset_id: str | None = None
    ragflow_document_id: str | None = None
    ragflow_task_id: str | None = None
    sync_status: str = "received"
    pipeline_status: str | None = None
    business_status: str = "active"
    current_version: int = 0
    attempt_count: int = 0
    next_retry_at: str | None = None
    batch_id: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_sync_at: str | None = None
    source_updated_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class OutboxEvent:
    event_id: str
    event_type: str
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    payload: str
    batch_id: str | None = None
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
    id: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_mapping(row: aiosqlite.Row) -> ExtDocumentMap:
    return ExtDocumentMap(
        id=row["id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        event_status=row["event_status"],
        sha256=row["sha256"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        bucket=row["bucket"],
        object_key=row["object_key"],
        ragflow_dataset_id=row["ragflow_dataset_id"],
        ragflow_document_id=row["ragflow_document_id"],
        ragflow_task_id=row["ragflow_task_id"],
        sync_status=row["sync_status"],
        pipeline_status=row["pipeline_status"],
        business_status=row["business_status"],
        current_version=row["current_version"],
        attempt_count=row["attempt_count"],
        next_retry_at=row["next_retry_at"],
        batch_id=row["batch_id"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        last_sync_at=row["last_sync_at"],
        source_updated_at=row["source_updated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_mapping(row: aiosqlite.Row) -> ExtDocumentMap:
    """Public row converter used by API/status layers."""
    return _row_to_mapping(row)


def _row_to_outbox(row: aiosqlite.Row) -> OutboxEvent:
    return OutboxEvent(
        id=row["id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        batch_id=row["batch_id"],
        payload=row["payload"],
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


async def migrate_schema(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(ext_document_map)") as cursor:
        rows = await cursor.fetchall()
    existing = {row["name"] for row in rows}
    for column, ddl in _MIGRATION_COLUMNS.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE ext_document_map ADD COLUMN {column} {ddl}")
    await db.executescript(CREATE_SYNC_OUTBOX)
    await db.commit()


async def init_db(db_path: str = "enterprise/ext_document_map.db") -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(CREATE_EXT_DOCUMENT_MAP)
    await migrate_schema(db)
    return db


async def insert_mapping(db: aiosqlite.Connection, doc: ExtDocumentMap) -> ExtDocumentMap:
    now = utc_now()
    try:
        cursor = await db.execute(
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, event_type, event_status, sha256, file_name, media_type,
                bucket, object_key, ragflow_dataset_id, ragflow_document_id,
                ragflow_task_id, sync_status, pipeline_status, business_status,
                current_version, attempt_count, next_retry_at, batch_id,
                last_error_code, last_error_message, last_sync_at,
                source_updated_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, source_system, external_document_id, source_version_id)
               DO NOTHING""",
            (
                doc.tenant_id, doc.source_system, doc.external_document_id,
                doc.source_version_id, doc.event_id, doc.event_type,
                doc.event_status, doc.sha256, doc.file_name, doc.media_type,
                doc.bucket, doc.object_key, doc.ragflow_dataset_id,
                doc.ragflow_document_id, doc.ragflow_task_id, doc.sync_status,
                doc.pipeline_status, doc.business_status, doc.current_version,
                doc.attempt_count, doc.next_retry_at, doc.batch_id,
                doc.last_error_code, doc.last_error_message, doc.last_sync_at,
                doc.source_updated_at, now, now,
            ),
        )
        await db.commit()
        if cursor.rowcount:
            doc.id = cursor.lastrowid
            doc.created_at = now
            doc.updated_at = now
            return doc
    except sqlite3.IntegrityError:
        # Unique event_id conflict with a different composite key: replay wins.
        await db.rollback()
        return await get_mapping_by_event_id(db, doc.event_id)
    existing = await get_mapping_by_event_id(db, doc.event_id)
    if existing:
        return existing
    return await get_mapping(
        db, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )


async def get_mapping(
    db: aiosqlite.Connection, tenant_id: str, source_system: str,
    external_document_id: str, source_version_id: str,
) -> ExtDocumentMap | None:
    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           AND source_version_id=?""",
        (tenant_id, source_system, external_document_id, source_version_id),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_mapping(row) if row else None


async def get_mapping_by_event_id(db: aiosqlite.Connection, event_id: str) -> ExtDocumentMap | None:
    async with db.execute(
        "SELECT * FROM ext_document_map WHERE event_id=?", (event_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_mapping(row) if row else None


async def get_mapping_by_sha(
    db: aiosqlite.Connection, tenant_id: str, dataset_id: str, sha256: str,
) -> ExtDocumentMap | None:
    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND ragflow_dataset_id=? AND sha256=?
           AND ragflow_document_id IS NOT NULL
           ORDER BY updated_at DESC LIMIT 1""",
        (tenant_id, dataset_id, sha256),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_mapping(row) if row else None


async def get_versions_for_document(
    db: aiosqlite.Connection, tenant_id: str, source_system: str,
    external_document_id: str,
) -> list[ExtDocumentMap]:
    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           ORDER BY updated_at DESC""",
        (tenant_id, source_system, external_document_id),
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_mapping(r) for r in rows]


async def list_mappings(
    db: aiosqlite.Connection,
    tenant_id: str | None = None,
    source_system: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = None,
    batch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    ascending: bool = False,
) -> list[ExtDocumentMap]:
    clauses: list[str] = []
    params: list[object] = []
    if tenant_id:
        clauses.append("tenant_id=?")
        params.append(tenant_id)
    if source_system:
        clauses.append("source_system=?")
        params.append(source_system)
    if status:
        clauses.append("sync_status=?")
        params.append(status)
    if statuses:
        clauses.append(
            f"sync_status IN ({', '.join('?' for _ in statuses)})"
        )
        params.extend(statuses)
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    order = "updated_at ASC" if ascending else "updated_at DESC"
    async with db.execute(
        f"""SELECT * FROM ext_document_map {where}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        params,
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_mapping(r) for r in rows]


async def update_mapping_status(
    db: aiosqlite.Connection,
    doc: ExtDocumentMap,
    sync_status: str,
    pipeline_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    event_status: str | None = None,
    business_status: str | None = None,
    current_version: int | None = None,
    attempt_count: int | None = None,
    next_retry_at: str | None = None,
    event_type: str | None = None,
    bucket: str | None = None,
    object_key: str | None = None,
) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE ext_document_map
           SET sync_status=?,
               pipeline_status=COALESCE(?, pipeline_status),
               last_error_code=?,
               last_error_message=?,
               event_status=COALESCE(?, event_status),
               business_status=COALESCE(?, business_status),
               current_version=COALESCE(?, current_version),
               attempt_count=COALESCE(?, attempt_count),
               next_retry_at=?,
               event_type=COALESCE(?, event_type),
               bucket=COALESCE(?, bucket),
               object_key=COALESCE(?, object_key),
               ragflow_dataset_id=COALESCE(?, ragflow_dataset_id),
               ragflow_document_id=COALESCE(?, ragflow_document_id),
               ragflow_task_id=COALESCE(?, ragflow_task_id),
               last_sync_at=?,
               updated_at=?
           WHERE id=?""",
        (
            sync_status, pipeline_status, error_code, error_message,
            event_status, business_status, current_version, attempt_count,
            next_retry_at, event_type, bucket, object_key,
            doc.ragflow_dataset_id, doc.ragflow_document_id,
            doc.ragflow_task_id, now, now, doc.id,
        ),
    )
    await db.commit()
    doc.sync_status = sync_status
    if pipeline_status is not None:
        doc.pipeline_status = pipeline_status
    doc.last_error_code = error_code
    doc.last_error_message = error_message
    if event_status is not None:
        doc.event_status = event_status
    if business_status is not None:
        doc.business_status = business_status
    if current_version is not None:
        doc.current_version = current_version
    if attempt_count is not None:
        doc.attempt_count = attempt_count
    if next_retry_at is not None:
        doc.next_retry_at = next_retry_at
    if event_type is not None:
        doc.event_type = event_type
    if bucket is not None:
        doc.bucket = bucket
    if object_key is not None:
        doc.object_key = object_key
    doc.last_sync_at = now
    doc.updated_at = now


async def supersede_other_versions(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    keep_source_version_id: str,
) -> list[ExtDocumentMap]:
    now = utc_now()
    await db.execute(
        """UPDATE ext_document_map
           SET sync_status='superseded', business_status='superseded',
               current_version=0, updated_at=?
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           AND source_version_id<>? AND business_status IN ('active', 'review_required')""",
        (now, tenant_id, source_system, external_document_id, keep_source_version_id),
    )
    await db.commit()
    return await get_versions_for_document(
        db, tenant_id, source_system, external_document_id,
    )


async def set_current_version(
    db: aiosqlite.Connection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> None:
    now = utc_now()
    await db.execute(
        """UPDATE ext_document_map
           SET current_version=CASE WHEN source_version_id=? THEN 1 ELSE 0 END,
               updated_at=?
           WHERE tenant_id=? AND source_system=? AND external_document_id=?""",
        (source_version_id, now, tenant_id, source_system, external_document_id),
    )
    await db.commit()


async def enqueue_outbox(db: aiosqlite.Connection, event: OutboxEvent) -> OutboxEvent:
    now = utc_now()
    try:
        cursor = await db.execute(
            """INSERT INTO sync_outbox
               (event_id, event_type, tenant_id, source_system,
                external_document_id, source_version_id, batch_id, payload,
                status, attempts, max_attempts, next_retry_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?)
               ON CONFLICT(event_id) DO NOTHING""",
            (
                event.event_id, event.event_type, event.tenant_id,
                event.source_system, event.external_document_id,
                event.source_version_id, event.batch_id, event.payload,
                event.max_attempts, now, now,
            ),
        )
        await db.commit()
        if cursor.rowcount:
            event.id = cursor.lastrowid
            event.status = "pending"
            event.created_at = now
            event.updated_at = now
            return event
    except sqlite3.IntegrityError:
        await db.rollback()
    existing = await get_outbox_by_event_id(db, event.event_id)
    return existing if existing else event


async def get_outbox_by_event_id(db: aiosqlite.Connection, event_id: str) -> OutboxEvent | None:
    async with db.execute(
        "SELECT * FROM sync_outbox WHERE event_id=?", (event_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_outbox(row) if row else None


async def claim_outbox(
    db: aiosqlite.Connection, worker_id: str, limit: int = 1,
) -> list[OutboxEvent]:
    now = utc_now()
    cursor = await db.execute(
        """UPDATE sync_outbox
           SET status='processing', locked_at=?, worker_id=?,
               attempts=attempts+1, updated_at=?
           WHERE id IN (
               SELECT id FROM sync_outbox
               WHERE status='pending'
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY created_at ASC
               LIMIT ?
           )""",
        (now, worker_id, now, now, limit),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return []
    async with db.execute(
        """SELECT * FROM sync_outbox
           WHERE worker_id=? AND locked_at=? AND status='processing'
           ORDER BY id LIMIT ?""",
        (worker_id, now, limit),
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_outbox(r) for r in rows]


async def mark_outbox_done(db: aiosqlite.Connection, event: OutboxEvent) -> None:
    await db.execute(
        """UPDATE sync_outbox
           SET status='done', locked_at=NULL, worker_id=NULL,
               next_retry_at=NULL, last_error_code=NULL, last_error_message=NULL,
               updated_at=?
           WHERE id=?""",
        (utc_now(), event.id),
    )
    await db.commit()
    event.status = "done"


async def mark_outbox_retry(
    db: aiosqlite.Connection, event: OutboxEvent,
    error_code: str | None, error_message: str | None,
) -> None:
    delay_seconds = min(2 ** max(event.attempts - 1, 0), 60)
    next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat()
    event.next_retry_at = next_retry_at
    # Attempts is already incremented by claim_outbox.
    await db.execute(
        """UPDATE sync_outbox
           SET status='pending', locked_at=NULL, worker_id=NULL,
               next_retry_at=?, last_error_code=?, last_error_message=?,
               updated_at=?
           WHERE id=?""",
        (next_retry_at, error_code, error_message, utc_now(), event.id),
    )
    await db.commit()
    event.status = "pending"
    event.last_error_code = error_code
    event.last_error_message = error_message


async def mark_outbox_failed(
    db: aiosqlite.Connection, event: OutboxEvent,
    error_code: str | None, error_message: str | None,
) -> None:
    status = "dead" if event.attempts >= event.max_attempts else "failed"
    await db.execute(
        """UPDATE sync_outbox
           SET status=?, locked_at=NULL, worker_id=NULL,
               last_error_code=?, last_error_message=?, updated_at=?
           WHERE id=?""",
        (status, error_code, error_message, utc_now(), event.id),
    )
    await db.commit()
    event.status = status
    event.last_error_code = error_code
    event.last_error_message = error_message


async def list_outbox_events(
    db: aiosqlite.Connection, status: str | None = None, limit: int = 100,
) -> list[OutboxEvent]:
    if status:
        async with db.execute(
            "SELECT * FROM sync_outbox WHERE status=? ORDER BY id LIMIT ?",
            (status, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM sync_outbox ORDER BY id LIMIT ?", (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_outbox(r) for r in rows]
