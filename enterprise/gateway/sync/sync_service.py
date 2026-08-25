"""Document sync orchestration: idempotent ingestion, retry, and lifecycle."""
import asyncio
import hashlib
import json
import logging
import os
from typing import Any

import aiosqlite

from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    OutboxEvent,
    get_mapping,
    get_mapping_by_event_id,
    get_versions_for_document,
    insert_mapping,
    list_all_mappings,
    promote_version_if_latest,
    reset_outbox_to_pending,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
)
from enterprise.gateway.sync.source_adapter import (
    SourceAdapter,
    SourceFetchError,
    SourceFile,
    SourceHashMismatch,
    SourceTooLarge,
)
from enterprise.gateway.sync.external_source import FileShareSourceAdapter
from enterprise.gateway.sync.state_machine import (
    is_terminal_document_status,
    transition_allowed,
    validate_transition,
)
from enterprise.gateway.sync.status_mapping import map_ragflow_run_to_sync_status

logger = logging.getLogger(__name__)

TERMINAL_DONE = frozenset({"ready", "superseded", "disabled", "deleted", "failed", "cancelled"})
RAGFLOW_UNSTARTED = frozenset({"", "0", "UNSTART"})


def _ragflow_file_name(doc: ExtDocumentMap, original_name: str) -> str:
    """Return a stable dataset-unique internal name, preserving the suffix."""

    dot = original_name.rfind(".")
    stem = original_name[:dot] if dot > 0 else original_name
    suffix = original_name[dot:] if dot > 0 else ""
    identity = "\n".join(
        (
            doc.tenant_id,
            doc.source_system,
            doc.external_document_id,
            doc.source_version_id,
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{stem[:80]}-{digest}{suffix}"


def _validate_ragflow_response(
    response: Any,
    operation: str,
    *,
    check_document_results: bool = False,
) -> dict:
    """Reject semantic API failures before changing Enterprise state."""
    if not isinstance(response, dict):
        raise RAGFlowAPIError(
            f"RAGFlow returned an invalid {operation} response", 0,
        )
    code = response.get("code")
    if code is not None and str(code) not in {"0", "200"}:
        raise RAGFlowAPIError(
            f"RAGFlow rejected {operation}", 400,
        )
    if check_document_results:
        data = response.get("data")
        if not isinstance(data, dict):
            raise RAGFlowAPIError(
                f"RAGFlow returned an invalid {operation} result", 0,
            )
        if any(
            isinstance(value, dict) and value.get("error")
            for value in data.values()
        ):
            raise RAGFlowAPIError(
                f"RAGFlow rejected one or more documents during {operation}", 400,
            )
    return response


async def promote_quality_passed_version(
    db: aiosqlite.Connection,
    ragflow_client: RAGFlowDocumentClient,
    doc: ExtDocumentMap,
    parse_quality_status: str,
) -> bool:
    """Promote only a quality-passed RAGFlow-owned version without an outage."""
    if (
        parse_quality_status != "passed"
        or doc.sync_status != "ready"
        or doc.business_status in {"disabled", "deleted", "superseded"}
        or not doc.ragflow_dataset_id
        or not doc.ragflow_document_id
    ):
        return False
    _validate_ragflow_response(
        await ragflow_client.batch_update_status(
            doc.ragflow_dataset_id, [doc.ragflow_document_id], enabled=True,
        ),
        "enable document",
        check_document_results=True,
    )
    if not await promote_version_if_latest(db, doc):
        # A newer version may have won the SQLite promotion transaction while
        # this quality job was running.  Never leave that stale RAGFlow
        # document enabled when its promotion was rejected.
        _validate_ragflow_response(
            await ragflow_client.batch_update_status(
                doc.ragflow_dataset_id, [doc.ragflow_document_id], enabled=False,
            ),
            "disable document",
            check_document_results=True,
        )
        return False
    versions = await get_versions_for_document(
        db, doc.tenant_id, doc.source_system, doc.external_document_id,
    )
    old_docs: dict[str, list[str]] = {}
    for version in versions:
        if (
            version.id != doc.id
            and version.ragflow_document_id
            and version.ragflow_dataset_id
            and version.business_status == "superseded"
        ):
            old_docs.setdefault(version.ragflow_dataset_id, []).append(
                version.ragflow_document_id
            )
    for dataset_id, document_ids in old_docs.items():
        _validate_ragflow_response(
            await ragflow_client.batch_update_status(
                dataset_id, document_ids, enabled=False,
            ),
            "disable superseded documents",
            check_document_results=True,
        )
    return True


class DocumentSyncError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RetryableDocumentSyncError(DocumentSyncError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=True)


class TerminalDocumentSyncError(DocumentSyncError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=False)


class DocumentNotFoundError(TerminalDocumentSyncError):
    def __init__(self):
        super().__init__("DOCUMENT_NOT_FOUND", "Document not found")


class SyncService:
    def __init__(
        self,
        db: aiosqlite.Connection,
        source_adapter: SourceAdapter,
        ragflow_client: RAGFlowDocumentClient,
        external_source_provider: FileShareSourceAdapter | None = None,
    ) -> None:
        self.db = db
        self.source_adapter = source_adapter
        self.ragflow_client = ragflow_client
        self.external_source_provider = external_source_provider

    async def process_event(
        self, event: OutboxEvent,
    ) -> tuple[ExtDocumentMap, bool]:
        payload = json.loads(event.payload)
        metadata = payload.get("metadata") or {}
        existing = await get_mapping_by_event_id(self.db, event.event_id)
        deduplicated = False
        if existing:
            if (
                existing.event_status == "completed"
                and existing.sync_status in TERMINAL_DONE
                and existing.sync_status != "deleted"
            ):
                return existing, True
            doc = existing
            if not doc.document_type and metadata.get("document_type"):
                await update_mapping_status(
                    self.db,
                    doc,
                    doc.sync_status,
                    document_type=metadata["document_type"],
                )
        else:
            doc = ExtDocumentMap(
                tenant_id=event.tenant_id,
                source_system=event.source_system,
                external_document_id=event.external_document_id,
                source_version_id=event.source_version_id,
                event_id=event.event_id,
                event_type=event.event_type,
                event_status="received",
                sha256=payload["sha256"],
                file_name=payload["fileName"],
                media_type=payload.get("mediaType", "application/pdf"),
                document_type=metadata.get("document_type"),
                source_page_count=metadata.get("page_count"),
                asset_id=(
                    metadata.get("asset_id")
                    or metadata.get("fixed_asset_no")
                    or metadata.get("equipment_id")
                ),
                equipment_id=metadata.get("equipment_id"),
                fixed_asset_no=metadata.get("fixed_asset_no"),
                department_id=metadata.get("department_id"),
                security_level=metadata.get("security_level"),
                allow_group_ids=json.dumps(
                    metadata.get("allow_group_ids") or [],
                    ensure_ascii=False,
                ),
                deny_group_ids=json.dumps(
                    metadata.get("deny_group_ids") or [],
                    ensure_ascii=False,
                ),
                source_kind=payload.get("source", {}).get("kind", "S3"),
                bucket=payload.get("source", {}).get("bucket", ""),
                object_key=payload.get("source", {}).get("objectKey", ""),
                storage_root_id=payload.get("source", {}).get("storageRootId"),
                relative_path=payload.get("source", {}).get("relativePath"),
                source_size=payload.get("source", {}).get("size"),
                source_etag=payload.get("source", {}).get("etag"),
                document_subtype=metadata.get("document_subtype"),
                source_document_type=metadata.get("source_document_type"),
                batch_id=event.batch_id,
                sync_status="received",
                business_status="active",
            )
            doc = await insert_mapping(self.db, doc)
            if doc.event_id != event.event_id:
                if doc.sha256.lower() != payload["sha256"].lower():
                    raise TerminalDocumentSyncError(
                        "DOCUMENT_VERSION_CONFLICT",
                        "Document version already has different content",
                    )
                return doc, True

        try:
            await self._sync_event(doc, payload, event)
            return doc, deduplicated
        except TerminalDocumentSyncError as e:
            if not is_terminal_document_status(doc.sync_status):
                failure_fields = {
                    "error_code": e.code,
                    "error_message": str(e),
                    "attempt_count": event.attempts,
                }
                if not doc.current_version:
                    failure_fields["business_status"] = "review_required"
                await self._set_status(
                    doc, "failed", event_status="failed",
                    **failure_fields,
                )
                await self._ensure_quality_evaluation(doc)
                await self._emit_terminal_failed_if_no_quality(doc)
            raise
        except RetryableDocumentSyncError as e:
            if doc.sync_status != "ready" and not is_terminal_document_status(doc.sync_status):
                await self._set_status(
                    doc, "retry_wait", event_status="retry_wait",
                    error_code=e.code, error_message=str(e),
                    attempt_count=event.attempts,
                )
            raise
        except Exception as e:
            logger.exception("Unexpected sync failure event_id=%s", event.event_id)
            raise RetryableDocumentSyncError(
                "INTERNAL_ERROR", "Unexpected sync failure"
            ) from e

    @staticmethod
    def _verify_source_file(
        source_file: SourceFile,
        expected_sha256: str,
        source_label: str,
    ) -> SourceFile:
        if not isinstance(source_file, SourceFile):
            raise SourceFetchError("Source adapter returned an invalid file")
        actual_sha256 = hashlib.sha256(source_file.content).hexdigest()
        if (
            source_file.sha256
            and source_file.sha256.lower() != actual_sha256
        ):
            raise SourceHashMismatch(
                f"SHA256 reported by source does not match {source_label}"
            )
        if actual_sha256 != expected_sha256.lower():
            raise SourceHashMismatch(f"SHA256 mismatch for {source_label}")
        source_file.sha256 = actual_sha256
        source_file.size = len(source_file.content)
        return source_file

    async def _sync_event(
        self,
        doc: ExtDocumentMap,
        payload: dict,
        event: OutboxEvent,
    ) -> None:
        if doc.sync_status == "ready" and doc.event_status == "tracking":
            await self._ensure_quality_evaluation(doc)
            return

        if doc.sync_status in ("received", "validated"):
            await self._set_status(
                doc, "validated", event_status="validating",
                attempt_count=event.attempts,
                ingest_state="VALIDATED" if doc.source_kind == "FILE_SHARE" else None,
            )

        source_file = None
        if not (
            doc.source_kind == "FILE_SHARE"
            or payload.get("source", {}).get("kind") == "FILE_SHARE"
        ):
            try:
                source_file = await self.source_adapter.fetch(
                    payload["source"]["bucket"],
                    payload["source"]["objectKey"],
                    payload["sha256"],
                )
                source_file = self._verify_source_file(
                    source_file,
                    payload["sha256"],
                    f"{payload['source']['bucket']}/{payload['source']['objectKey']}",
                )
            except SourceHashMismatch as e:
                raise TerminalDocumentSyncError("DOCUMENT_HASH_MISMATCH", str(e)) from e
            except SourceTooLarge as e:
                raise TerminalDocumentSyncError("DOCUMENT_SOURCE_NOT_FOUND", str(e)) from e
            except SourceFetchError as e:
                raise RetryableDocumentSyncError("DOCUMENT_SOURCE_NOT_FOUND", str(e)) from e

        if doc.sync_status in ("received", "validated", "failed", "retry_wait"):
            await self._set_status(
                doc, "accepted", event_status="transferring",
                attempt_count=event.attempts,
                ingest_state="TRANSFERRING",
                source_state="AVAILABLE" if doc.source_kind == "FILE_SHARE" else None,
                source_state_reason="" if doc.source_kind == "FILE_SHARE" else None,
            )
        elif doc.sync_status in ("cancelled", "deleted"):
            await self._set_status(
                doc, "registered", event_status="transferring",
                attempt_count=event.attempts,
                business_status="active",
            )

        dataset = await self._ensure_dataset(doc.tenant_id)
        dataset_id = dataset.get("id") or dataset.get("data", {}).get("id", "")
        if not dataset_id:
            raise RetryableDocumentSyncError(
                "RAGFLOW_UNAVAILABLE", "Dataset id missing from RAGFlow response"
            )
        if doc.sync_status in (
            "received", "validated", "accepted", "transferring",
            "retry_wait", "failed", "cancelled",
        ):
            if not doc.ragflow_dataset_id:
                doc.ragflow_dataset_id = dataset_id
            await self._set_status(
                doc, "registered", event_status="registering",
                attempt_count=event.attempts,
                ingest_state="REGISTERED" if doc.source_kind == "FILE_SHARE" else None,
            )
        elif not doc.ragflow_dataset_id:
            doc.ragflow_dataset_id = dataset_id
            await self._set_status(
                doc, "registered", event_status="registering",
                attempt_count=event.attempts,
            )

        doc = await self._register_ragflow(
            doc, payload, source_file, dataset_id, event,
        )
        mapped = map_ragflow_run_to_sync_status(doc.pipeline_status)
        if mapped in {"ready", "failed"} and await self._retry_technical_parse_once(
            doc, doc.pipeline_status or "",
        ):
            return
        if mapped == "ready":
            await self._set_status(
                doc, "ready", event_status="completed",
                pipeline_status=doc.pipeline_status,
                business_status="active",
                ingest_state="READY",
            )
            await self._ensure_quality_evaluation(doc)
        elif mapped == "failed":
            failure_fields = {
                "pipeline_status": doc.pipeline_status,
                "error_code": "DOCUMENT_PARSE_FAILED",
                "error_message": "RAGFlow parsing failed",
                "event_status": "failed",
            }
            if not doc.current_version:
                failure_fields["business_status"] = "review_required"
            await self._set_status(doc, "failed", **failure_fields)
            await self._ensure_quality_evaluation(doc)
            await self._emit_terminal_failed_if_no_quality(doc)
        else:
            await self._set_status(
                doc, mapped, event_status="completed",
                pipeline_status=doc.pipeline_status,
            )

    async def _set_status(
        self,
        doc: ExtDocumentMap,
        sync_status: str,
        event_status: str | None = None,
        **kwargs: Any,
    ) -> None:
        if doc.sync_status != sync_status:
            validate_transition(doc.sync_status, sync_status, "document")
        await update_mapping_status(
            self.db, doc, sync_status, event_status=event_status, **kwargs,
        )

    async def _emit_terminal_failed_if_no_quality(self, doc: ExtDocumentMap) -> None:
        """Emit failed only when the quality worker will not produce a terminal."""
        from enterprise.gateway.callback_delivery import emit_terminal_callback_safe
        from enterprise.gateway.config import config

        if config.quality_worker_enabled and doc.ragflow_document_id:
            return
        code = doc.last_error_code or "DOCUMENT_SYNC_FAILED"
        message = doc.last_error_message or "Document synchronization failed"
        await emit_terminal_callback_safe(
            self.db,
            doc,
            "failed",
            quality_status=None,
            retrievable=False,
            error={"code": code, "message": message, "retryable": False},
        )

    async def _ensure_dataset(self, tenant_id: str) -> dict:
        name = (
            os.environ.get("ENTERPRISE_RAGFLOW_DATASET_NAME", "").strip()
            or f"enterprise-{tenant_id}"
        )
        permission = os.environ.get(
            "ENTERPRISE_RAGFLOW_DATASET_PERMISSION", ""
        ).strip().lower()
        if permission not in {"", "me", "team"}:
            raise TerminalDocumentSyncError(
                "RAGFLOW_DATASET_PERMISSION_INVALID",
                "ENTERPRISE_RAGFLOW_DATASET_PERMISSION must be 'me' or 'team'",
            )
        try:
            return _validate_ragflow_response(
                await self.ragflow_client.find_or_create_dataset(
                    name,
                    permission=permission or None,
                ),
                "find or create dataset",
            )
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e

    async def _register_ragflow(
        self,
        doc: ExtDocumentMap,
        payload: dict,
        source_file,
        dataset_id: str,
        event: OutboxEvent,
    ) -> ExtDocumentMap:
        if not doc.ragflow_document_id:
            existing_doc = await self._find_document_by_event(
                dataset_id, event.event_id,
            )
            if existing_doc:
                doc.ragflow_document_id = existing_doc["id"]
                doc.ragflow_dataset_id = dataset_id
                doc.pipeline_status = existing_doc.get("run") or "UNSTART"
            else:
                try:
                    if doc.source_kind == "FILE_SHARE":
                        result = _validate_ragflow_response(
                            await self._upload_file_share_document(
                                doc, payload, dataset_id,
                            ),
                            "register document",
                        )
                    else:
                        result = _validate_ragflow_response(
                            await self.ragflow_client.upload_document(
                                dataset_id,
                                _ragflow_file_name(doc, payload["fileName"]),
                                source_file.content,
                            ),
                            "register document",
                        )
                except RAGFlowAPIError as e:
                    raise self._ragflow_error(e) from e
                docs_data = result.get("data", [])
                if isinstance(docs_data, dict):
                    docs_data = [docs_data] if docs_data.get("id") else []
                if not isinstance(docs_data, list) or not docs_data:
                    raise RetryableDocumentSyncError(
                        "DOCUMENT_SYNC_FAILED", "RAGFlow returned no document"
                    )
                ragflow_doc = docs_data[0]
                if not isinstance(ragflow_doc, dict) or not ragflow_doc.get("id"):
                    raise RetryableDocumentSyncError(
                        "DOCUMENT_SYNC_FAILED", "RAGFlow document id is missing"
                    )
                doc.ragflow_dataset_id = dataset_id
                doc.ragflow_document_id = ragflow_doc.get("id", "")
                doc.ragflow_task_id = ragflow_doc.get("id", "")
                doc.pipeline_status = ragflow_doc.get("run") or "UNSTART"

        # Persist the RAGFlow document id before the optional metadata write so
        # an interrupted retry can reuse the uploaded document instead of
        # creating a duplicate knowledge version.
        await update_mapping_status(
            self.db, doc, doc.sync_status,
            pipeline_status=doc.pipeline_status,
        )

        ragflow_doc = await self._ensure_enterprise_metadata(
            doc, dataset_id, event,
        )
        doc.pipeline_status = ragflow_doc.get("run") or doc.pipeline_status or "UNSTART"

        run = (doc.pipeline_status or "UNSTART").upper()
        if run in RAGFLOW_UNSTARTED:
            try:
                _validate_ragflow_response(
                    await self.ragflow_client.start_parsing(
                        dataset_id, [doc.ragflow_document_id],
                    ),
                    "start parsing",
                )
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e
            doc.pipeline_status = "RUNNING"
            await update_mapping_status(
                self.db, doc, doc.sync_status,
                pipeline_status=doc.pipeline_status,
            )

        try:
            docs = await self.ragflow_client.list_documents(
                dataset_id, document_id=doc.ragflow_document_id,
            )
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e
        readback_found = False
        for rf_doc in docs:
            if rf_doc.get("id") == doc.ragflow_document_id:
                readback_found = True
                doc.pipeline_status = rf_doc.get("run") or doc.pipeline_status or "UNSTART"
                break
        if not readback_found:
            raise RetryableDocumentSyncError(
                "RAGFLOW_UNAVAILABLE",
                "RAGFlow document readback is empty",
            )

        await update_mapping_status(
            self.db, doc, doc.sync_status,
            pipeline_status=doc.pipeline_status,
        )
        return doc

    async def _upload_file_share_document(
        self,
        doc: ExtDocumentMap,
        payload: dict,
        dataset_id: str,
    ) -> dict:
        if self.external_source_provider is None:
            raise RetryableDocumentSyncError(
                "DOCUMENT_SOURCE_NOT_FOUND",
                "FILE_SHARE source provider is not configured",
            )
        source = payload.get("source") or {}
        expected_size = source.get("size")
        if expected_size is None:
            expected_size = doc.source_size
        try:
            handle = await asyncio.to_thread(
                self.external_source_provider.open_verified,
                source.get("storageRootId") or doc.storage_root_id or "",
                source.get("relativePath") or doc.relative_path or "",
                payload.get("sha256") or doc.sha256,
                expected_size=expected_size,
                expected_etag=source.get("etag") or doc.source_etag,
            )
        except SourceFetchError as exc:
            await update_mapping_status(
                self.db,
                doc,
                doc.sync_status,
                source_state="UNAVAILABLE",
                source_state_reason=type(exc).__name__,
            )
            if isinstance(exc, SourceHashMismatch):
                raise TerminalDocumentSyncError(
                    "DOCUMENT_HASH_MISMATCH", str(exc),
                ) from exc
            if isinstance(exc, SourceTooLarge):
                raise TerminalDocumentSyncError(
                    "DOCUMENT_SOURCE_NOT_FOUND", str(exc),
                ) from exc
            raise RetryableDocumentSyncError(
                "DOCUMENT_SOURCE_NOT_FOUND", str(exc),
            ) from exc
        try:
            return await self.ragflow_client.upload_document(
                dataset_id,
                _ragflow_file_name(doc, payload.get("fileName") or doc.file_name),
                handle,
            )
        finally:
            await asyncio.to_thread(handle.close)

    @staticmethod
    def _external_meta_fields(doc: ExtDocumentMap, event: OutboxEvent) -> dict:
        # Asset identifiers are registration metadata (document provenance),
        # not OCR ground truth. A scan may legitimately omit both identifiers.
        # Keep the quality declaration explicit and scalar so RAGFlow's
        # metadata update API can preserve it. The older
        # ``...ground_truth_fields`` key is already mapped as an object in
        # some RAGFlow indices, so use a new scalar key instead of changing
        # that field's type.
        ground_truth_fields = {}
        required_capabilities = ["text", "position"]
        meta = {
            "enterprise_event_id": event.event_id,
            "enterprise_external_document_id": doc.external_document_id,
            "enterprise_source_version_id": doc.source_version_id,
            "enterprise_sha256": doc.sha256,
            "enterprise_document_type": doc.document_type,
            "enterprise_quality_expected_tables": [],
            "enterprise_quality_ground_truth_json": json.dumps(
                ground_truth_fields, separators=(",", ":")
            ),
            "enterprise_quality_citation_expected": False,
            "enterprise_quality_required_capabilities": required_capabilities,
        }
        # Gateway canonical identity only — prove document provenance, not
        # that OCR content contains these values.
        if doc.equipment_id:
            meta["equipment_id"] = doc.equipment_id
        if doc.fixed_asset_no:
            meta["fixed_asset_no"] = doc.fixed_asset_no
        return meta

    async def _ensure_enterprise_metadata(
        self,
        doc: ExtDocumentMap,
        dataset_id: str,
        event: OutboxEvent,
    ) -> dict[str, Any]:
        try:
            docs = await self.ragflow_client.list_documents(
                dataset_id, document_id=doc.ragflow_document_id,
            )
        except RAGFlowAPIError as exc:
            raise self._ragflow_error(exc) from exc
        if not docs:
            raise RetryableDocumentSyncError(
                "RAGFLOW_UNAVAILABLE", "RAGFlow document readback is empty",
            )
        current = docs[0]
        run = str(current.get("run") or "UNSTART").upper()
        if run not in RAGFLOW_UNSTARTED:
            return current
        enterprise_meta = self._external_meta_fields(doc, event)
        current_meta = current.get("meta_fields")
        if not isinstance(current_meta, dict):
            current_meta = {}
        if all(current_meta.get(key) == value for key, value in enterprise_meta.items()):
            return current
        try:
            _validate_ragflow_response(
                await self.ragflow_client.update_document_metadata(
                    dataset_id,
                    doc.ragflow_document_id,
                    {**current_meta, **enterprise_meta},
                ),
                "upsert enterprise metadata",
            )
            docs = await self.ragflow_client.list_documents(
                dataset_id, document_id=doc.ragflow_document_id,
            )
        except RAGFlowAPIError as exc:
            raise self._ragflow_error(exc) from exc
        if not docs:
            raise RetryableDocumentSyncError(
                "RAGFLOW_UNAVAILABLE", "RAGFlow metadata readback is empty",
            )
        return docs[0]

    async def _has_usable_chunks(self, doc: ExtDocumentMap) -> bool:
        page = 1
        page_size = 100
        while True:
            try:
                response = _validate_ragflow_response(
                    await self.ragflow_client.list_chunks(
                        doc.ragflow_dataset_id,
                        doc.ragflow_document_id,
                        page=page,
                        page_size=page_size,
                    ),
                    "read parsed chunks",
                )
            except RAGFlowAPIError as exc:
                raise self._ragflow_error(exc) from exc
            data = response.get("data") or {}
            chunks = data.get("chunks") or []
            if any(str(chunk.get("content") or "").strip() for chunk in chunks):
                return True
            total = int(data.get("total") or 0)
            if not chunks or page * page_size >= total or len(chunks) < page_size:
                return False
            page += 1

    async def _retry_technical_parse_once(
        self,
        doc: ExtDocumentMap,
        run: str,
    ) -> bool:
        if doc.parse_retry_count >= 1:
            return False
        normalized_run = str(run or "").upper()
        reason = None
        if normalized_run in {"FAIL", "4"}:
            reason = "RAGFLOW_PARSE_FAILED"
        elif normalized_run in {"DONE", "3"}:
            if await self._has_usable_chunks(doc):
                return False
            reason = "RAGFLOW_EMPTY_RESULT"
        if reason is None:
            return False
        try:
            _validate_ragflow_response(
                await self.ragflow_client.start_parsing(
                    doc.ragflow_dataset_id, [doc.ragflow_document_id],
                ),
                "retry parsing",
            )
        except RAGFlowAPIError as exc:
            raise self._ragflow_error(exc) from exc
        target_status = (
            "queued"
            if transition_allowed(doc.sync_status, "queued", "document")
            else doc.sync_status
        )
        await self._set_status(
            doc,
            target_status,
            event_status="tracking",
            pipeline_status="RUNNING",
            parse_retry_count=doc.parse_retry_count + 1,
        )
        logger.info(
            "RAGFlow technical parse retry started document=%s version=%s reason=%s",
            doc.external_document_id,
            doc.source_version_id,
            reason,
        )
        return True

    async def _find_document_by_event(
        self, dataset_id: str, event_id: str,
    ) -> dict | None:
        try:
            docs = await self.ragflow_client.list_documents(dataset_id)
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e
        for rf_doc in docs:
            meta = rf_doc.get("meta_fields") or {}
            if meta.get("enterprise_event_id") == event_id:
                return rf_doc
        return None

    async def promote_quality_passed_version(
        self, doc: ExtDocumentMap, parse_quality_status: str,
    ) -> bool:
        try:
            return await promote_quality_passed_version(
                self.db, self.ragflow_client, doc, parse_quality_status,
            )
        except RAGFlowAPIError as exc:
            raise self._ragflow_error(exc) from exc

    async def _ensure_quality_evaluation(self, doc: ExtDocumentMap) -> None:
        from enterprise.gateway.config import config
        from enterprise.gateway.quality.models import get_or_create_evaluation

        try:
            await get_or_create_evaluation(
                self.db,
                tenant_id=doc.tenant_id,
                source_system=doc.source_system,
                external_document_id=doc.external_document_id,
                source_version_id=doc.source_version_id,
                ragflow_dataset_id=doc.ragflow_dataset_id,
                ragflow_document_id=doc.ragflow_document_id,
                evaluation_version="1",
                max_attempts=config.quality_max_attempts,
            )
        except Exception:
            logger.exception(
                "Quality evaluation enqueue failed document=%s version=%s",
                doc.external_document_id,
                doc.source_version_id,
            )
            if doc.sync_status == "ready":
                raise RetryableDocumentSyncError(
                    "QUALITY_EVALUATION_ENQUEUE_FAILED",
                    "Quality evaluation could not be queued",
                )

    async def refresh_status(self, doc: ExtDocumentMap) -> ExtDocumentMap:
        if (
            not doc.ragflow_dataset_id
            or not doc.ragflow_document_id
            or doc.sync_status in ("superseded", "disabled", "deleted")
        ):
            return doc
        try:
            docs = await self.ragflow_client.list_documents(
                doc.ragflow_dataset_id,
                document_id=doc.ragflow_document_id,
            )
        except RAGFlowAPIError as exc:
            raise self._ragflow_error(exc) from exc
        readback_found = False
        for rf_doc in docs:
            if rf_doc.get("id") != doc.ragflow_document_id:
                continue
            readback_found = True
            run = rf_doc.get("run") or "UNSTART"
            mapped = map_ragflow_run_to_sync_status(run)
            if mapped in {"ready", "failed"} and await self._retry_technical_parse_once(
                doc, run,
            ):
                break
            if mapped == "ready":
                if doc.sync_status != "ready":
                    await self._set_status(
                        doc,
                        "ready",
                        event_status="completed",
                        pipeline_status=run,
                        business_status="active",
                    )
                    await self._ensure_quality_evaluation(doc)
                else:
                    await update_mapping_status(
                        self.db, doc, "ready",
                        pipeline_status=run,
                        event_status="completed",
                    )
            elif mapped == "failed":
                failure_fields = {
                    "pipeline_status": run,
                    "error_code": "DOCUMENT_PARSE_FAILED",
                    "error_message": "RAGFlow parsing failed",
                    "event_status": "failed",
                }
                if not doc.current_version:
                    failure_fields["business_status"] = "review_required"
                await self._set_status(doc, "failed", **failure_fields)
                await self._ensure_quality_evaluation(doc)
                await self._emit_terminal_failed_if_no_quality(doc)
            elif (
                doc.sync_status != mapped
                and transition_allowed(doc.sync_status, mapped, "document")
            ):
                await self._set_status(
                    doc, mapped, event_status="completed", pipeline_status=run,
                )
            break
        if not readback_found:
            return await self.mark_ragflow_document_missing(doc)
        return await get_mapping(
            self.db, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.source_version_id,
        ) or doc

    async def mark_ragflow_document_missing(
        self, doc: ExtDocumentMap,
    ) -> ExtDocumentMap:
        """Mirror a RAGFlow UI/API deletion into Gateway mapping state."""
        if doc.sync_status in ("superseded", "disabled", "deleted"):
            if doc.sync_status == "deleted" and doc.ragflow_document_id:
                await self._clear_ragflow_binding(doc)
            return await get_mapping(
                self.db, doc.tenant_id, doc.source_system,
                doc.external_document_id, doc.source_version_id,
            ) or doc
        if transition_allowed(doc.sync_status, "deleted", "document"):
            target = "deleted"
            business_status = "deleted"
        elif transition_allowed(doc.sync_status, "failed", "document"):
            target = "failed"
            business_status = doc.business_status
        else:
            raise RetryableDocumentSyncError(
                "RAGFLOW_UNAVAILABLE",
                "RAGFlow document readback is empty",
            )
        await self._set_status(
            doc,
            target,
            event_status="completed",
            business_status=business_status,
            error_code="RAGFLOW_DOCUMENT_MISSING",
            error_message="Document was removed from RAGFlow",
        )
        await self._clear_ragflow_binding(doc)
        return await get_mapping(
            self.db, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.source_version_id,
        ) or doc

    async def _clear_ragflow_binding(self, doc: ExtDocumentMap) -> None:
        await self.db.execute(
            """UPDATE ext_document_map
                  SET ragflow_document_id=NULL,
                      ragflow_task_id=NULL
                WHERE id=?""",
            (doc.id,),
        )
        await self.db.commit()
        doc.ragflow_document_id = None
        doc.ragflow_task_id = None

    async def reconcile_missing_ragflow_documents(self) -> int:
        mappings = await list_all_mappings(
            self.db, statuses=["ready", "review_required"],
        )
        by_dataset: dict[str, list[ExtDocumentMap]] = {}
        for doc in mappings:
            if doc.ragflow_dataset_id and doc.ragflow_document_id:
                by_dataset.setdefault(doc.ragflow_dataset_id, []).append(doc)
        marked = 0
        for dataset_id, docs in by_dataset.items():
            try:
                present = await self._ragflow_document_ids(dataset_id)
            except RAGFlowAPIError:
                continue
            for doc in docs:
                if doc.ragflow_document_id not in present:
                    await self.mark_ragflow_document_missing(doc)
                    marked += 1
        return marked

    async def _ragflow_document_ids(self, dataset_id: str) -> set[str]:
        ids: set[str] = set()
        page = 1
        while True:
            docs = await self.ragflow_client.list_documents(
                dataset_id, page=page, page_size=100,
            )
            for item in docs:
                doc_id = item.get("id")
                if doc_id:
                    ids.add(doc_id)
            if len(docs) < 100:
                return ids
            page += 1

    def _needs_ragflow_reingest(self, doc: ExtDocumentMap) -> bool:
        if doc.sync_status == "deleted":
            return True
        return (
            doc.sync_status in {"ready", "review_required", "failed"}
            and not doc.ragflow_document_id
        )

    async def ensure_present_or_requeue(
        self, doc: ExtDocumentMap,
    ) -> tuple[ExtDocumentMap, bool]:
        """If RAGFlow no longer has the doc, mark deleted and re-queue ingest."""
        if (
            doc.sync_status in {"ready", "review_required"}
            and doc.ragflow_dataset_id
            and doc.ragflow_document_id
        ):
            try:
                docs = await self.ragflow_client.list_documents(
                    doc.ragflow_dataset_id,
                    document_id=doc.ragflow_document_id,
                )
            except RAGFlowAPIError:
                docs = None
            if docs is not None and not any(
                item.get("id") == doc.ragflow_document_id for item in docs
            ):
                doc = await self.mark_ragflow_document_missing(doc)
        if not self._needs_ragflow_reingest(doc):
            return doc, False
        return await self.requeue_after_ragflow_delete(doc), True

    async def requeue_after_ragflow_delete(
        self, doc: ExtDocumentMap,
    ) -> ExtDocumentMap:
        if doc.sync_status != "deleted":
            doc = await self.mark_ragflow_document_missing(doc)
        if doc.sync_status == "deleted":
            await self._set_status(
                doc,
                "registered",
                event_status="received",
                business_status="active",
                error_code=None,
                error_message=None,
            )
        await reset_outbox_to_pending(self.db, doc.event_id)
        return await get_mapping(
            self.db, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.source_version_id,
        ) or doc

    async def disable_document(
        self, tenant_id: str, source_system: str, external_document_id: str,
    ) -> list[ExtDocumentMap]:
        versions = await get_versions_for_document(
            self.db, tenant_id, source_system, external_document_id,
        )
        if not versions:
            raise DocumentNotFoundError()
        await self._set_ragflow_enabled(versions, False)
        for version in versions:
            await update_mapping_status(
                self.db, version, "disabled",
                business_status="disabled", event_status="completed",
            )
        return versions

    async def reindex_document(
        self,
        tenant_id: str,
        source_system: str,
        external_document_id: str,
        source_version_id: str,
    ) -> ExtDocumentMap:
        doc = await get_mapping(
            self.db,
            tenant_id,
            source_system,
            external_document_id,
            source_version_id,
        )
        if not doc:
            raise DocumentNotFoundError()
        if not doc.ragflow_dataset_id or not doc.ragflow_document_id:
            raise TerminalDocumentSyncError(
                "DOCUMENT_NOT_READY", "Document is not registered in RAGFlow"
            )
        try:
            _validate_ragflow_response(
                await self.ragflow_client.start_parsing(
                    doc.ragflow_dataset_id, [doc.ragflow_document_id]
                ),
                "start parsing",
            )
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e
        await self._set_status(
            doc,
            doc.sync_status,
            event_status="tracking",
            pipeline_status="RUNNING",
            event_type="reindex",
            parse_retry_count=0,
        )
        return doc

    async def restore_document(
        self, tenant_id: str, source_system: str, external_document_id: str,
    ) -> ExtDocumentMap:
        versions = await get_versions_for_document(
            self.db, tenant_id, source_system, external_document_id,
        )
        if not versions:
            raise DocumentNotFoundError()
        doc = max(versions, key=lambda v: (v.current_version, v.updated_at or ""))
        payload = {
            "sha256": doc.sha256,
            "fileName": doc.file_name,
            "mediaType": doc.media_type,
            "source": (
                {
                    "kind": "FILE_SHARE",
                    "storageRootId": doc.storage_root_id,
                    "relativePath": doc.relative_path,
                    "size": doc.source_size,
                    "etag": doc.source_etag,
                }
                if doc.source_kind == "FILE_SHARE"
                else {"bucket": doc.bucket, "objectKey": doc.object_key}
            ),
        }
        source_file = None
        if doc.source_kind != "FILE_SHARE":
            try:
                source_file = await self.source_adapter.fetch(
                    doc.bucket, doc.object_key, doc.sha256,
                )
                source_file = self._verify_source_file(
                    source_file,
                    doc.sha256,
                    f"{doc.bucket}/{doc.object_key}",
                )
            except SourceHashMismatch as e:
                raise TerminalDocumentSyncError("DOCUMENT_HASH_MISMATCH", str(e)) from e
            except SourceFetchError as e:
                raise RetryableDocumentSyncError("DOCUMENT_SOURCE_NOT_FOUND", str(e)) from e

        if doc.sync_status != "ready":
            await self._set_status(
                doc, "registered", event_status="transferring",
                business_status="active",
            )
        dataset = await self._ensure_dataset(doc.tenant_id)
        dataset_id = dataset.get("id") or dataset.get("data", {}).get("id", "")
        if doc.ragflow_document_id:
            try:
                _validate_ragflow_response(
                    await self.ragflow_client.batch_update_status(
                        dataset_id, [doc.ragflow_document_id], True,
                    ),
                    "restore document",
                    check_document_results=True,
                )
                docs = await self.ragflow_client.list_documents(dataset_id)
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e
            readback_found = False
            for rf_doc in docs:
                if rf_doc.get("id") == doc.ragflow_document_id:
                    readback_found = True
                    doc.pipeline_status = rf_doc.get("run") or doc.pipeline_status or "UNSTART"
                    break
            if not readback_found:
                raise RetryableDocumentSyncError(
                    "RAGFLOW_UNAVAILABLE",
                    "RAGFlow restore readback is empty",
                )
            await update_mapping_status(
                self.db, doc, doc.sync_status, business_status="active",
                event_status="completed",
            )
        else:
            event = OutboxEvent(
                event_id=doc.event_id,
                event_type=doc.event_type,
                tenant_id=doc.tenant_id,
                source_system=doc.source_system,
                external_document_id=doc.external_document_id,
                source_version_id=doc.source_version_id,
                payload=json.dumps(payload),
                batch_id=doc.batch_id,
            )
            doc = await self._register_ragflow(
                doc, payload, source_file, dataset_id, event,
            )
        mapped = map_ragflow_run_to_sync_status(doc.pipeline_status)
        if mapped in {"ready", "failed"} and await self._retry_technical_parse_once(
            doc, doc.pipeline_status or "",
        ):
            return doc
        if mapped == "ready":
            await self._set_status(
                doc,
                "ready",
                event_status="completed",
                pipeline_status=doc.pipeline_status,
                business_status="active",
            )
            await self._ensure_quality_evaluation(doc)
        else:
            await update_mapping_status(
                self.db, doc, doc.sync_status, event_status="completed",
            )
        return doc

    async def delete_document(
        self, tenant_id: str, source_system: str, external_document_id: str,
    ) -> list[ExtDocumentMap]:
        versions = await get_versions_for_document(
            self.db, tenant_id, source_system, external_document_id,
        )
        if not versions:
            raise DocumentNotFoundError()
        await self._set_ragflow_enabled(versions, False)
        for version in versions:
            await update_mapping_status(
                self.db, version, "deleted",
                business_status="deleted", event_status="completed",
            )
        return versions

    async def _set_ragflow_enabled(
        self, versions: list[ExtDocumentMap], enabled: bool,
    ) -> None:
        by_dataset: dict[str, list[str]] = {}
        for version in versions:
            if version.ragflow_document_id and version.ragflow_dataset_id:
                by_dataset.setdefault(version.ragflow_dataset_id, []).append(
                    version.ragflow_document_id
                )
        for dataset_id, document_ids in by_dataset.items():
            try:
                _validate_ragflow_response(
                    await self.ragflow_client.batch_update_status(
                        dataset_id, document_ids, enabled,
                    ),
                    "update document status",
                    check_document_results=True,
                )
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e

    @staticmethod
    def _ragflow_error(e: RAGFlowAPIError) -> DocumentSyncError:
        if e.status_code and 400 <= e.status_code < 500:
            return TerminalDocumentSyncError(
                "RAGFLOW_API_INCOMPATIBLE", "RAGFlow API request rejected"
            )
        return RetryableDocumentSyncError(
            "RAGFLOW_UNAVAILABLE", "RAGFlow service is temporarily unavailable"
        )
