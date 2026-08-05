"""Enterprise sync database models and migration."""
import aiosqlite
from dataclasses import dataclass, field
from datetime import datetime, timezone

CREATE_EXT_DOCUMENT_MAP = """
CREATE TABLE IF NOT EXISTS ext_document_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    external_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_name TEXT NOT NULL,
    media_type TEXT DEFAULT "application/pdf",
    ragflow_dataset_id TEXT,
    ragflow_document_id TEXT,
    ragflow_task_id TEXT,
    sync_status TEXT NOT NULL DEFAULT "received",
    pipeline_status TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    last_sync_at TEXT,
    source_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT "",
    updated_at TEXT NOT NULL DEFAULT "",
    UNIQUE(tenant_id, source_system, external_document_id, source_version_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ext_doc_event
    ON ext_document_map(event_id);

CREATE INDEX IF NOT EXISTS idx_ext_doc_status
    ON ext_document_map(tenant_id, sync_status);
"""


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
    ragflow_dataset_id: str | None = None
    ragflow_document_id: str | None = None
    ragflow_task_id: str | None = None
    sync_status: str = "received"
    pipeline_status: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_sync_at: str | None = None
    source_updated_at: str | None = None
    id: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


async def init_db(db_path: str = "enterprise/ext_document_map.db") -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(CREATE_EXT_DOCUMENT_MAP)
    await db.commit()
    return db


async def insert_mapping(db: aiosqlite.Connection, doc: ExtDocumentMap) -> ExtDocumentMap:
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        """INSERT INTO ext_document_map
           (tenant_id, source_system, external_document_id, source_version_id,
            event_id, sha256, file_name, media_type,
            ragflow_dataset_id, ragflow_document_id, ragflow_task_id,
            sync_status, pipeline_status, last_error_code, last_error_message,
            last_sync_at, source_updated_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tenant_id, source_system, external_document_id, source_version_id)
           DO NOTHING""",
        (doc.tenant_id, doc.source_system, doc.external_document_id, doc.source_version_id,
         doc.event_id, doc.sha256, doc.file_name, doc.media_type,
         doc.ragflow_dataset_id, doc.ragflow_document_id, doc.ragflow_task_id,
         doc.sync_status, doc.pipeline_status, doc.last_error_code, doc.last_error_message,
         doc.last_sync_at, doc.source_updated_at, now, now)
    )
    await db.commit()
    if cursor.rowcount == 0:
        # Already exists, fetch existing
        return await get_mapping(db, doc.tenant_id, doc.source_system, doc.external_document_id, doc.source_version_id)
    doc.id = cursor.lastrowid
    return doc


async def get_mapping(
    db: aiosqlite.Connection, tenant_id: str, source_system: str,
    external_document_id: str, source_version_id: str
) -> ExtDocumentMap | None:
    async with db.execute(
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           AND source_version_id=?""",
        (tenant_id, source_system, external_document_id, source_version_id)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return ExtDocumentMap(
                id=row["id"], tenant_id=row["tenant_id"], source_system=row["source_system"],
                external_document_id=row["external_document_id"],
                source_version_id=row["source_version_id"],
                event_id=row["event_id"], sha256=row["sha256"],
                file_name=row["file_name"], media_type=row["media_type"],
                ragflow_dataset_id=row["ragflow_dataset_id"],
                ragflow_document_id=row["ragflow_document_id"],
                ragflow_task_id=row["ragflow_task_id"],
                sync_status=row["sync_status"], pipeline_status=row["pipeline_status"],
                last_error_code=row["last_error_code"],
                last_error_message=row["last_error_message"],
                last_sync_at=row["last_sync_at"],
                source_updated_at=row["source_updated_at"]
            )
    return None


async def get_mapping_by_event_id(db: aiosqlite.Connection, event_id: str) -> ExtDocumentMap | None:
    async with db.execute(
        "SELECT * FROM ext_document_map WHERE event_id=?", (event_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return ExtDocumentMap(
                id=row["id"], tenant_id=row["tenant_id"], source_system=row["source_system"],
                external_document_id=row["external_document_id"],
                source_version_id=row["source_version_id"],
                event_id=row["event_id"], sha256=row["sha256"],
                file_name=row["file_name"], media_type=row["media_type"],
                ragflow_dataset_id=row["ragflow_dataset_id"],
                ragflow_document_id=row["ragflow_document_id"],
                ragflow_task_id=row["ragflow_task_id"],
                sync_status=row["sync_status"], pipeline_status=row["pipeline_status"],
                last_error_code=row["last_error_code"],
                last_error_message=row["last_error_message"],
                last_sync_at=row["last_sync_at"],
                source_updated_at=row["source_updated_at"]
            )
    return None


async def update_mapping_status(
    db: aiosqlite.Connection, doc: ExtDocumentMap,
    sync_status: str, pipeline_status: str | None = None,
    error_code: str | None = None, error_message: str | None = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE ext_document_map
           SET sync_status=?, pipeline_status=?,
               last_error_code=?, last_error_message=?,
               ragflow_dataset_id=COALESCE(?, ragflow_dataset_id),
               ragflow_document_id=COALESCE(?, ragflow_document_id),
               ragflow_task_id=COALESCE(?, ragflow_task_id),
               last_sync_at=?, updated_at=?
           WHERE id=?""",
        (sync_status, pipeline_status, error_code, error_message,
         doc.ragflow_dataset_id, doc.ragflow_document_id, doc.ragflow_task_id,
         now, now, doc.id)
    )
    await db.commit()
