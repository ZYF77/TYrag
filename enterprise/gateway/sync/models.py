"""Enterprise sync database models and outbox store."""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.db.exceptions import PersistenceConflictError

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
    document_type: str | None = None
    source_page_count: int | None = None
    event_type: str = "upsert"
    event_status: str = "received"
    processing_round: int = 1
    source_kind: str = "S3"
    bucket: str = ""
    object_key: str = ""
    storage_root_id: str | None = None
    relative_path: str | None = None
    source_size: int | None = None
    source_modified_ns: int | None = None
    source_etag: str | None = None
    asset_id: str | None = None
    equipment_id: str | None = None
    fixed_asset_no: str | None = None
    department_id: str | None = None
    security_level: int | None = None
    allow_group_ids: str | None = None
    deny_group_ids: str | None = None
    ragflow_dataset_id: str | None = None
    ragflow_document_id: str | None = None
    ragflow_task_id: str | None = None
    sync_status: str = "received"
    pipeline_status: str | None = None
    business_status: str = "active"
    current_version: int = 0
    parser_profile: str | None = None
    parser_profile_version: str | None = None
    parser_expected_json: str | None = None
    parser_configured_json: str | None = None
    parser_executed_json: str | None = None
    parser_application_status: str = "legacy_unverified"
    document_subtype: str | None = None
    source_document_type: str | None = None
    ingest_state: str = "RECEIVED"
    source_state: str = "AVAILABLE"
    source_state_reason: str | None = None
    attempt_count: int = 0
    parse_retry_count: int = 0
    next_retry_at: str | None = None
    batch_id: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_error_retryable: bool = False
    last_sync_at: str | None = None
    parsed_at: str | None = None
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
    processing_round: int = 1
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


@dataclass(frozen=True)
class DocumentEventReceipt:
    event_id: str
    payload_hash: str
    tenant_id: str
    source_system: str
    external_document_id: str
    source_version_id: str
    outcome_code: str
    created_at: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_mapping(row: Mapping[str, Any]) -> ExtDocumentMap:
    return ExtDocumentMap(
        id=row["id"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        event_status=row["event_status"],
        processing_round=int(row["processing_round"] or 1),
        sha256=row["sha256"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        document_type=row["document_type"],
        source_page_count=row["source_page_count"],
        source_kind=row["source_kind"] or "S3",
        bucket=row["bucket"],
        object_key=row["object_key"],
        storage_root_id=row["storage_root_id"],
        relative_path=row["relative_path"],
        source_size=row["source_size"],
        source_modified_ns=row["source_modified_ns"],
        source_etag=row["source_etag"],
        asset_id=row["asset_id"],
        equipment_id=row["equipment_id"],
        fixed_asset_no=row["fixed_asset_no"],
        department_id=row["department_id"],
        security_level=row["security_level"],
        allow_group_ids=row["allow_group_ids"],
        deny_group_ids=row["deny_group_ids"],
        ragflow_dataset_id=row["ragflow_dataset_id"],
        ragflow_document_id=row["ragflow_document_id"],
        ragflow_task_id=row["ragflow_task_id"],
        sync_status=row["sync_status"],
        pipeline_status=row["pipeline_status"],
        business_status=row["business_status"],
        current_version=row["current_version"],
        parser_profile=row["parser_profile"],
        parser_profile_version=row["parser_profile_version"],
        parser_expected_json=row["parser_expected_json"],
        parser_configured_json=row["parser_configured_json"],
        parser_executed_json=row["parser_executed_json"],
        parser_application_status=(
            row["parser_application_status"] or "legacy_unverified"
        ),
        document_subtype=row["document_subtype"],
        source_document_type=row["source_document_type"],
        ingest_state=row["ingest_state"] or "RECEIVED",
        source_state=row["source_state"] or "AVAILABLE",
        source_state_reason=row["source_state_reason"],
        attempt_count=row["attempt_count"],
        parse_retry_count=row["parse_retry_count"],
        next_retry_at=row["next_retry_at"],
        batch_id=row["batch_id"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        last_error_retryable=bool(row["last_error_retryable"]),
        last_sync_at=row["last_sync_at"],
        parsed_at=row["parsed_at"],
        source_updated_at=row["source_updated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_mapping(row: Mapping[str, Any]) -> ExtDocumentMap:
    """Public row converter used by API/status layers."""
    return _row_to_mapping(row)


def _row_to_outbox(row: Mapping[str, Any]) -> OutboxEvent:
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
        processing_round=int(row["processing_round"] or 1),
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


async def insert_mapping(
    conn: AsyncConnection,
    doc: ExtDocumentMap, *,
    return_inserted: bool = False,
) -> ExtDocumentMap | tuple[ExtDocumentMap, bool]:
    now = utc_now()
    try:
        result = await exec_sql(conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                 event_id, event_type, event_status, sha256, file_name, media_type,
                 document_type,
                 source_page_count, source_kind, bucket, object_key,
                 storage_root_id, relative_path, source_size,
                 source_modified_ns, source_etag, asset_id,
                 equipment_id, fixed_asset_no,
                department_id, security_level, allow_group_ids, deny_group_ids,
                ragflow_dataset_id, ragflow_document_id,
                ragflow_task_id, sync_status, pipeline_status, business_status,
                current_version, parser_profile, parser_profile_version,
                 parser_expected_json, parser_configured_json,
                 parser_executed_json, parser_application_status,
                 document_subtype, source_document_type, ingest_state,
                 source_state, source_state_reason,
                 attempt_count, next_retry_at, batch_id,
                last_error_code, last_error_message, last_sync_at,
                source_updated_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, source_system, external_document_id, source_version_id)
               DO NOTHING
               RETURNING id""",
            (
                doc.tenant_id, doc.source_system, doc.external_document_id,
                doc.source_version_id, doc.event_id, doc.event_type,
                 doc.event_status, doc.sha256, doc.file_name, doc.media_type,
                 doc.document_type,
                 doc.source_page_count, doc.source_kind, doc.bucket, doc.object_key,
                 doc.storage_root_id, doc.relative_path, doc.source_size,
                 doc.source_modified_ns, doc.source_etag,
                 doc.asset_id,
                doc.equipment_id, doc.fixed_asset_no,
                doc.department_id, doc.security_level,
                doc.allow_group_ids, doc.deny_group_ids,
                doc.ragflow_dataset_id, doc.ragflow_document_id,
                doc.ragflow_task_id, doc.sync_status, doc.pipeline_status,
                doc.business_status, doc.current_version,
                doc.parser_profile, doc.parser_profile_version,
                 doc.parser_expected_json, doc.parser_configured_json,
                 doc.parser_executed_json, doc.parser_application_status,
                 doc.document_subtype, doc.source_document_type,
                 doc.ingest_state, doc.source_state, doc.source_state_reason,
                 doc.attempt_count,
                doc.next_retry_at, doc.batch_id, doc.last_error_code,
                doc.last_error_message, doc.last_sync_at, doc.source_updated_at,
                now, now,
            ),
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is not None:
            doc.id = int(inserted_id)
            doc.created_at = now
            doc.updated_at = now
            # The test registry is an explicit offline fixture only.  It is
            # never populated from document metadata in production mode.
            if os.environ.get("ENTERPRISE_TEST_MODE") == "1" and (
                doc.equipment_id or doc.fixed_asset_no or doc.asset_id
            ):
                result = await exec_sql(conn,
                    """INSERT INTO ext_asset_registry
                       (tenant_id, equipment_id, fixed_asset_no, asset_id)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(tenant_id, equipment_id) DO UPDATE SET
                         fixed_asset_no=excluded.fixed_asset_no,
                         asset_id=excluded.asset_id""",
                    (
                        doc.tenant_id,
                        doc.equipment_id or doc.fixed_asset_no or doc.asset_id,
                        doc.fixed_asset_no,
                        doc.asset_id or doc.fixed_asset_no or doc.equipment_id,
                    ),
                )
            return (doc, True) if return_inserted else doc
    except PersistenceConflictError:
        # Unique event_id conflict with a different composite key: replay wins.
        pass
    existing = await get_mapping_by_event_id(conn, doc.event_id)
    if existing:
        return (existing, False) if return_inserted else existing
    existing = await get_mapping(
        conn, doc.tenant_id, doc.source_system,
        doc.external_document_id, doc.source_version_id,
    )
    return (existing, False) if return_inserted else existing


async def get_mapping(
    conn: AsyncConnection, tenant_id: str, source_system: str,
    external_document_id: str, source_version_id: str,
) -> ExtDocumentMap | None:
    row = await fetchone(
        conn,
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           AND source_version_id=?""",
        (tenant_id, source_system, external_document_id, source_version_id),
    )
    return _row_to_mapping(row) if row else None


async def get_mapping_by_event_id(conn: AsyncConnection, event_id: str) -> ExtDocumentMap | None:
    row = await fetchone(
        conn, "SELECT * FROM ext_document_map WHERE event_id=?", (event_id,),
    )
    return _row_to_mapping(row) if row else None


async def get_document_event_receipt(
    conn: AsyncConnection, event_id: str,
) -> DocumentEventReceipt | None:
    row = await fetchone(
        conn,
        "SELECT * FROM ext_document_event_receipt WHERE event_id=?",
        (event_id,),
    )
    if not row:
        return None
    return DocumentEventReceipt(
        event_id=row["event_id"],
        payload_hash=row["payload_hash"],
        tenant_id=row["tenant_id"],
        source_system=row["source_system"],
        external_document_id=row["external_document_id"],
        source_version_id=row["source_version_id"],
        outcome_code=row["outcome_code"],
        created_at=row["created_at"],
    )


async def insert_document_event_receipt(
    conn: AsyncConnection,
    receipt: DocumentEventReceipt,
) -> DocumentEventReceipt:
    result = await exec_sql(conn,
        """INSERT INTO ext_document_event_receipt
           (event_id, payload_hash, tenant_id, source_system,
            external_document_id, source_version_id, outcome_code, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO NOTHING""",
        (
            receipt.event_id,
            receipt.payload_hash,
            receipt.tenant_id,
            receipt.source_system,
            receipt.external_document_id,
            receipt.source_version_id,
            receipt.outcome_code,
            receipt.created_at or utc_now(),
        ),
    )
    existing = await get_document_event_receipt(conn, receipt.event_id)
    if existing is None:
        raise RuntimeError("Document event receipt insert failed")
    return existing


async def get_mapping_by_sha(
    conn: AsyncConnection, tenant_id: str, dataset_id: str, sha256: str,
) -> ExtDocumentMap | None:
    row = await fetchone(
        conn,
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND ragflow_dataset_id=? AND sha256=?
           AND ragflow_document_id IS NOT NULL
           ORDER BY updated_at DESC LIMIT 1""",
        (tenant_id, dataset_id, sha256),
    )
    return _row_to_mapping(row) if row else None


async def get_versions_for_document(
    conn: AsyncConnection, tenant_id: str, source_system: str,
    external_document_id: str,
) -> list[ExtDocumentMap]:
    rows = await fetchall(
        conn,
        """SELECT * FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           ORDER BY updated_at DESC""",
        (tenant_id, source_system, external_document_id),
    )
    return [_row_to_mapping(r) for r in rows]


async def list_mappings(
    conn: AsyncConnection,
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
    rows = await fetchall(
        conn,
        f"""SELECT * FROM ext_document_map {where}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        params,
    )
    return [_row_to_mapping(r) for r in rows]


async def list_all_mappings(
    conn: AsyncConnection,
    tenant_id: str | None = None,
    source_system: str | None = None,
    statuses: list[str] | None = None,
    batch_id: str | None = None,
    page_size: int = 100,
) -> list[ExtDocumentMap]:
    """Read every matching mapping without the list_mappings page cap."""
    docs: list[ExtDocumentMap] = []
    offset = 0
    while True:
        batch = await list_mappings(
            conn,
            tenant_id=tenant_id,
            source_system=source_system,
            statuses=statuses,
            batch_id=batch_id,
            limit=page_size,
            offset=offset,
            ascending=True,
        )
        docs.extend(batch)
        if len(batch) < page_size:
            return docs
        offset += page_size


async def update_mapping_status(
    conn: AsyncConnection,
    doc: ExtDocumentMap,
    sync_status: str,
    pipeline_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    event_status: str | None = None,
    business_status: str | None = None,
    current_version: int | None = None,
    attempt_count: int | None = None,
    parse_retry_count: int | None = None,
    next_retry_at: str | None = None,
    last_error_retryable: bool = False,
    expected_processing_round: int | None = None,
    event_type: str | None = None,
    document_type: str | None = None,
    source_page_count: int | None = None,
    bucket: str | None = None,
    object_key: str | None = None,
    asset_id: str | None = None,
    department_id: str | None = None,
    security_level: int | None = None,
    allow_group_ids: str | None = None,
    deny_group_ids: str | None = None,
    ingest_state: str | None = None,
    source_state: str | None = None,
    source_state_reason: str | None = None,
) -> bool:
    now = utc_now()
    # parsed_at 只在"转入 ready"那一刻写入：已 ready 再调用不覆盖；
    # failed→重新注册→ready 的重试轮次会自然刷新。存量数据不回填。
    parsed_at_value = (
        now if (sync_status == "ready" and doc.sync_status != "ready") else None
    )
    result = await exec_sql(conn,
        """UPDATE ext_document_map
           SET sync_status=?,
               pipeline_status=COALESCE(?, pipeline_status),
               last_error_code=?,
               last_error_message=?,
               last_error_retryable=?,
               event_status=COALESCE(?, event_status),
               business_status=COALESCE(?, business_status),
                current_version=COALESCE(?, current_version),
                attempt_count=COALESCE(?, attempt_count),
                parse_retry_count=COALESCE(?, parse_retry_count),
                next_retry_at=?,
                event_type=COALESCE(?, event_type),
                document_type=COALESCE(?, document_type),
                source_page_count=COALESCE(?, source_page_count),
               bucket=COALESCE(?, bucket),
               object_key=COALESCE(?, object_key),
               asset_id=COALESCE(?, asset_id),
               department_id=COALESCE(?, department_id),
               security_level=COALESCE(?, security_level),
                allow_group_ids=COALESCE(?, allow_group_ids),
                deny_group_ids=COALESCE(?, deny_group_ids),
                ingest_state=COALESCE(?, ingest_state),
                source_state=COALESCE(?, source_state),
                source_state_reason=COALESCE(?, source_state_reason),
               ragflow_dataset_id=COALESCE(?, ragflow_dataset_id),
               ragflow_document_id=COALESCE(?, ragflow_document_id),
               ragflow_task_id=COALESCE(?, ragflow_task_id),
               parsed_at=COALESCE(?, parsed_at),
               last_sync_at=?,
               updated_at=?
           WHERE id=? AND (CAST(? AS INTEGER) IS NULL OR processing_round=?)""",
        (
             sync_status, pipeline_status, error_code, error_message,
             1 if last_error_retryable else 0,
             event_status, business_status, current_version, attempt_count,
             parse_retry_count, next_retry_at, event_type, document_type, source_page_count,
             bucket, object_key,
             asset_id,
             department_id, security_level, allow_group_ids, deny_group_ids,
             ingest_state, source_state, source_state_reason,
             doc.ragflow_dataset_id, doc.ragflow_document_id,
             doc.ragflow_task_id, parsed_at_value, now, now, doc.id,
             expected_processing_round, expected_processing_round,
         ),
    )
    if expected_processing_round is not None and not result.rowcount:
        return False
    doc.sync_status = sync_status
    if pipeline_status is not None:
        doc.pipeline_status = pipeline_status
    doc.last_error_code = error_code
    doc.last_error_message = error_message
    doc.last_error_retryable = bool(last_error_retryable)
    if event_status is not None:
        doc.event_status = event_status
    if business_status is not None:
        doc.business_status = business_status
    if current_version is not None:
        doc.current_version = current_version
    if attempt_count is not None:
        doc.attempt_count = attempt_count
    if parse_retry_count is not None:
        doc.parse_retry_count = parse_retry_count
    if next_retry_at is not None:
        doc.next_retry_at = next_retry_at
    if event_type is not None:
        doc.event_type = event_type
    if document_type is not None:
        doc.document_type = document_type
    if source_page_count is not None:
        doc.source_page_count = source_page_count
    if bucket is not None:
        doc.bucket = bucket
    if object_key is not None:
        doc.object_key = object_key
    if asset_id is not None:
        doc.asset_id = asset_id
    if department_id is not None:
        doc.department_id = department_id
    if security_level is not None:
        doc.security_level = security_level
    if allow_group_ids is not None:
        doc.allow_group_ids = allow_group_ids
    if deny_group_ids is not None:
        doc.deny_group_ids = deny_group_ids
    if ingest_state is not None:
        doc.ingest_state = ingest_state
    if source_state is not None:
        doc.source_state = source_state
    if source_state_reason is not None:
        doc.source_state_reason = source_state_reason
    doc.last_sync_at = now
    doc.updated_at = now
    if parsed_at_value is not None:
        doc.parsed_at = parsed_at_value
    return True


async def claim_failed_processing_round(
    conn: AsyncConnection,
    doc_id: int,
) -> ExtDocumentMap | None:
    """Atomically claim one retryable terminal failure for reprocessing."""
    now = utc_now()
    result = await exec_sql(
        conn,
        """UPDATE ext_document_map
              SET processing_round=processing_round+1,
                  sync_status='registered',
                  event_status='accepted',
                  pipeline_status='UNSTART',
                  business_status='active',
                  last_error_code=NULL,
                  last_error_message=NULL,
                  last_error_retryable=0,
                  parse_retry_count=0,
                  next_retry_at=NULL,
                  updated_at=?,
                  last_sync_at=?
            WHERE id=?
              AND sync_status='failed'
              AND last_error_retryable=1
              AND business_status NOT IN ('disabled', 'deleted', 'superseded')
            RETURNING *""",
        (now, now, doc_id),
    )
    row = result.mappings().first()
    if row is None:
        return None
    mapping = _row_to_mapping(dict(row))
    await exec_sql(
        conn,
        """UPDATE sync_outbox
              SET processing_round=processing_round+1,
                  status='pending', locked_at=NULL, worker_id=NULL,
                  attempts=0, next_retry_at=NULL,
                  last_error_code=NULL, last_error_message=NULL,
                  updated_at=?
            WHERE event_id=?""",
        (now, mapping.event_id),
    )
    return mapping


async def update_parser_application(
    conn: AsyncConnection,
    doc: ExtDocumentMap,
    *,
    status: str,
    profile: str | None = None,
    profile_version: str | None = None,
    expected_json: str | None = None,
    configured_json: str | None = None,
    executed_json: str | None = None,
) -> None:
    now = utc_now()
    result = await exec_sql(conn,
        """UPDATE ext_document_map
           SET parser_profile=COALESCE(?, parser_profile),
               parser_profile_version=COALESCE(?, parser_profile_version),
               parser_expected_json=COALESCE(?, parser_expected_json),
               parser_configured_json=COALESCE(?, parser_configured_json),
               parser_executed_json=COALESCE(?, parser_executed_json),
               parser_application_status=?, updated_at=?
           WHERE id=?""",
        (
            profile, profile_version, expected_json, configured_json,
            executed_json, status, now, doc.id,
        ),
    )
    if profile is not None:
        doc.parser_profile = profile
    if profile_version is not None:
        doc.parser_profile_version = profile_version
    if expected_json is not None:
        doc.parser_expected_json = expected_json
    if configured_json is not None:
        doc.parser_configured_json = configured_json
    if executed_json is not None:
        doc.parser_executed_json = executed_json
    doc.parser_application_status = status
    doc.updated_at = now


async def promote_version_if_latest(
    conn: AsyncConnection, doc: ExtDocumentMap,
) -> bool:
    """Atomically promote the latest eligible version; safe to call repeatedly."""
    now = utc_now()
    latest = await fetchone(
        conn,
        """SELECT id FROM ext_document_map
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
             AND business_status NOT IN ('disabled', 'deleted')
           ORDER BY id DESC LIMIT 1""",
        (doc.tenant_id, doc.source_system, doc.external_document_id),
    )
    if latest is None or latest["id"] != doc.id:
        return False
    await exec_sql(
        conn,
        """UPDATE ext_document_map
           SET sync_status='superseded', business_status='superseded',
               current_version=0, updated_at=?
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
             AND id<>? AND business_status IN ('active', 'review_required')""",
        (
            now, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.id,
        ),
    )
    await exec_sql(
        conn,
        """UPDATE ext_document_map
           SET sync_status='ready', business_status='active',
               current_version=1, event_status='completed', updated_at=?
           WHERE id=?""",
        (now, doc.id),
    )
    doc.sync_status = "ready"
    doc.business_status = "active"
    doc.current_version = 1
    doc.event_status = "completed"
    doc.updated_at = now
    return True


async def supersede_other_versions(
    conn: AsyncConnection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    keep_source_version_id: str,
) -> list[ExtDocumentMap]:
    now = utc_now()
    result = await exec_sql(conn,
        """UPDATE ext_document_map
           SET sync_status='superseded', business_status='superseded',
               current_version=0, updated_at=?
           WHERE tenant_id=? AND source_system=? AND external_document_id=?
           AND source_version_id<>? AND business_status IN ('active', 'review_required')""",
        (now, tenant_id, source_system, external_document_id, keep_source_version_id),
    )
    return await get_versions_for_document(
        conn, tenant_id, source_system, external_document_id,
    )


async def set_current_version(
    conn: AsyncConnection,
    tenant_id: str,
    source_system: str,
    external_document_id: str,
    source_version_id: str,
) -> None:
    now = utc_now()
    result = await exec_sql(conn,
        """UPDATE ext_document_map
           SET current_version=CASE WHEN source_version_id=? THEN 1 ELSE 0 END,
               updated_at=?
           WHERE tenant_id=? AND source_system=? AND external_document_id=?""",
        (source_version_id, now, tenant_id, source_system, external_document_id),
    )


async def enqueue_outbox(
    conn: AsyncConnection,
    event: OutboxEvent,
) -> OutboxEvent:
    now = utc_now()
    try:
        result = await exec_sql(conn,
            """INSERT INTO sync_outbox
               (event_id, event_type, tenant_id, source_system,
                external_document_id, source_version_id, processing_round,
                batch_id, payload,
                status, attempts, max_attempts, next_retry_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?)
               ON CONFLICT(event_id) DO NOTHING
               RETURNING id""",
            (
                event.event_id, event.event_type, event.tenant_id,
                event.source_system, event.external_document_id,
                event.source_version_id, event.processing_round,
                event.batch_id, event.payload,
                event.max_attempts, now, now,
            ),
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is not None:
            event.id = int(inserted_id)
            event.status = "pending"
            event.created_at = now
            event.updated_at = now
            return event
    except PersistenceConflictError:
        pass
    existing = await get_outbox_by_event_id(conn, event.event_id)
    return existing if existing else event


async def get_outbox_by_event_id(conn: AsyncConnection, event_id: str) -> OutboxEvent | None:
    row = await fetchone(
        conn, "SELECT * FROM sync_outbox WHERE event_id=?", (event_id,),
    )
    return _row_to_outbox(row) if row else None


async def claim_outbox(
    conn: AsyncConnection, worker_id: str, limit: int = 1,
) -> list[OutboxEvent]:
    now = utc_now()
    rows = await fetchall(
        conn,
        """WITH candidates AS (
               SELECT id FROM sync_outbox
               WHERE status='pending'
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY created_at ASC, id ASC
               LIMIT ? FOR UPDATE SKIP LOCKED
           )
           UPDATE sync_outbox AS o
              SET status='processing', locked_at=?, worker_id=?,
                  attempts=attempts+1, updated_at=?
             FROM candidates
            WHERE o.id=candidates.id
           RETURNING o.*""",
        (now, limit, now, worker_id, now),
    )
    return [_row_to_outbox(r) for r in rows]


async def mark_outbox_done(conn: AsyncConnection, event: OutboxEvent) -> None:
    result = await exec_sql(conn,
        """UPDATE sync_outbox
           SET status='done', locked_at=NULL, worker_id=NULL,
               next_retry_at=NULL, last_error_code=NULL, last_error_message=NULL,
               updated_at=?
           WHERE id=? AND processing_round=? AND status='processing'
             AND worker_id=? AND locked_at=?""",
        (
            utc_now(), event.id, event.processing_round,
            event.worker_id, event.locked_at,
        ),
    )
    if result.rowcount:
        event.status = "done"


async def reset_outbox_to_pending(
    conn: AsyncConnection, event_id: str,
) -> OutboxEvent | None:
    """Re-queue a completed/failed outbox row so ingest can run again."""
    existing = await get_outbox_by_event_id(conn, event_id)
    if not existing or not existing.id:
        return existing
    now = utc_now()
    result = await exec_sql(conn,
        """UPDATE sync_outbox
           SET status='pending', locked_at=NULL, worker_id=NULL,
               attempts=0, next_retry_at=NULL,
               last_error_code=NULL, last_error_message=NULL,
               updated_at=?
           WHERE id=?""",
        (now, existing.id),
    )
    existing.status = "pending"
    existing.attempts = 0
    existing.locked_at = None
    existing.worker_id = None
    existing.next_retry_at = None
    existing.last_error_code = None
    existing.last_error_message = None
    existing.updated_at = now
    return existing


async def mark_outbox_retry(
    conn: AsyncConnection, event: OutboxEvent,
    error_code: str | None, error_message: str | None,
) -> None:
    delay_seconds = min(2 ** max(event.attempts - 1, 0), 60)
    next_retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat()
    event.next_retry_at = next_retry_at
    # Attempts is already incremented by claim_outbox.
    result = await exec_sql(conn,
        """UPDATE sync_outbox
           SET status='pending', locked_at=NULL, worker_id=NULL,
               next_retry_at=?, last_error_code=?, last_error_message=?,
               updated_at=?
           WHERE id=? AND processing_round=? AND status='processing'
             AND worker_id=? AND locked_at=?""",
        (
            next_retry_at, error_code, error_message, utc_now(), event.id,
            event.processing_round, event.worker_id, event.locked_at,
        ),
    )
    if result.rowcount:
        event.status = "pending"
        event.last_error_code = error_code
        event.last_error_message = error_message


async def mark_outbox_failed(
    conn: AsyncConnection, event: OutboxEvent,
    error_code: str | None, error_message: str | None,
) -> None:
    status = "dead" if event.attempts >= event.max_attempts else "failed"
    result = await exec_sql(conn,
        """UPDATE sync_outbox
           SET status=?, locked_at=NULL, worker_id=NULL,
               last_error_code=?, last_error_message=?, updated_at=?
           WHERE id=? AND processing_round=? AND status='processing'
             AND worker_id=? AND locked_at=?""",
        (
            status, error_code, error_message, utc_now(), event.id,
            event.processing_round, event.worker_id, event.locked_at,
        ),
    )
    if result.rowcount:
        event.status = status
        event.last_error_code = error_code
        event.last_error_message = error_message


async def clear_ragflow_binding(conn: AsyncConnection, doc: ExtDocumentMap) -> None:
    await exec_sql(
        conn,
        """UPDATE ext_document_map
              SET ragflow_document_id=NULL,
                  ragflow_task_id=NULL
            WHERE id=?""",
        (doc.id,),
    )
    doc.ragflow_document_id = None
    doc.ragflow_task_id = None


async def list_outbox_events(
    conn: AsyncConnection, status: str | None = None, limit: int = 100,
) -> list[OutboxEvent]:
    if status:
        rows = await fetchall(
            conn,
            "SELECT * FROM sync_outbox WHERE status=? ORDER BY id LIMIT ?",
            (status, limit),
        )
    else:
        rows = await fetchall(
            conn,
            "SELECT * FROM sync_outbox ORDER BY id LIMIT ?",
            (limit,),
        )
    return [_row_to_outbox(r) for r in rows]
