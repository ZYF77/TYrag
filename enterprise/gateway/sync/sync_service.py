"""Document sync orchestration: idempotent ingestion, retry, and lifecycle."""
import json
import logging
from typing import Any

import aiosqlite

from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    OutboxEvent,
    get_mapping,
    get_mapping_by_event_id,
    get_versions_for_document,
    insert_mapping,
    set_current_version,
    supersede_other_versions,
    update_mapping_status,
)
from enterprise.gateway.sync.ragflow_document_client import (
    RAGFlowAPIError,
    RAGFlowDocumentClient,
)
from enterprise.gateway.sync.source_adapter import (
    SourceAdapter,
    SourceFetchError,
    SourceHashMismatch,
    SourceTooLarge,
)
from enterprise.gateway.sync.state_machine import (
    is_terminal_document_status,
    transition_allowed,
    validate_transition,
)
from enterprise.gateway.sync.status_mapping import map_ragflow_run_to_sync_status

logger = logging.getLogger(__name__)

TERMINAL_DONE = frozenset({"ready", "superseded", "disabled", "deleted", "failed", "cancelled"})


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
    ) -> None:
        self.db = db
        self.source_adapter = source_adapter
        self.ragflow_client = ragflow_client

    async def process_event(
        self, event: OutboxEvent,
    ) -> tuple[ExtDocumentMap, bool]:
        payload = json.loads(event.payload)
        existing = await get_mapping_by_event_id(self.db, event.event_id)
        deduplicated = False
        if existing:
            if existing.event_status == "completed" and existing.sync_status in TERMINAL_DONE:
                return existing, True
            doc = existing
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
                source_page_count=(payload.get("metadata") or {}).get("page_count"),
                bucket=payload["source"]["bucket"],
                object_key=payload["source"]["objectKey"],
                batch_id=event.batch_id,
                sync_status="received",
                business_status="active",
            )
            doc = await insert_mapping(self.db, doc)
            if doc.event_id != event.event_id:
                existing = doc
                if existing.event_status == "completed" and existing.sync_status in TERMINAL_DONE:
                    return existing, True

        try:
            await self._sync_event(doc, payload, event)
            return doc, deduplicated
        except TerminalDocumentSyncError as e:
            if not is_terminal_document_status(doc.sync_status):
                await self._set_status(
                    doc, "failed", event_status="failed",
                    error_code=e.code, error_message=str(e),
                    attempt_count=event.attempts,
                )
                await self._ensure_quality_evaluation(doc)
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

    async def _sync_event(
        self,
        doc: ExtDocumentMap,
        payload: dict,
        event: OutboxEvent,
    ) -> None:
        if doc.sync_status == "ready" and doc.event_status == "tracking":
            await self._activate_version(doc)
            return

        if doc.sync_status in ("received", "validated"):
            await self._set_status(
                doc, "validated", event_status="validating",
                attempt_count=event.attempts,
            )

        try:
            source_file = await self.source_adapter.fetch(
                payload["source"]["bucket"],
                payload["source"]["objectKey"],
                payload["sha256"],
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
            )
        elif doc.sync_status == "cancelled":
            await self._set_status(
                doc, "registered", event_status="transferring",
                attempt_count=event.attempts,
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
        if mapped == "ready":
            await self._set_status(
                doc, "ready", event_status="tracking",
                pipeline_status=doc.pipeline_status,
            )
            await self._activate_version(doc)
        else:
            await self._set_status(
                doc, mapped, event_status="completed",
                pipeline_status=doc.pipeline_status,
            )
            if mapped == "failed":
                await self._ensure_quality_evaluation(doc)

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

    async def _ensure_dataset(self, tenant_id: str) -> dict:
        name = f"enterprise-{tenant_id}"
        try:
            return await self.ragflow_client.find_or_create_dataset(name)
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
            else:
                try:
                    result = await self.ragflow_client.upload_document(
                        dataset_id, payload["fileName"], source_file.content,
                    )
                except RAGFlowAPIError as e:
                    raise self._ragflow_error(e) from e
                docs_data = result.get("data", [])
                if not docs_data:
                    raise RetryableDocumentSyncError(
                        "DOCUMENT_SYNC_FAILED", "RAGFlow returned no document"
                    )
                ragflow_doc = docs_data[0]
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

        meta_fields = {
            "enterprise_event_id": event.event_id,
            "enterprise_external_document_id": doc.external_document_id,
            "enterprise_source_version_id": doc.source_version_id,
            "enterprise_sha256": doc.sha256,
        }
        try:
            await self.ragflow_client.update_document_metadata(
                dataset_id, doc.ragflow_document_id, meta_fields,
            )
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e

        run = (doc.pipeline_status or "UNSTART").upper()
        if run not in ("DONE", "3"):
            try:
                await self.ragflow_client.start_parsing(
                    dataset_id, [doc.ragflow_document_id],
                )
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e
            doc.pipeline_status = "RUNNING"
            await update_mapping_status(
                self.db, doc, doc.sync_status,
                pipeline_status=doc.pipeline_status,
            )

        try:
            docs = await self.ragflow_client.list_documents(dataset_id)
        except RAGFlowAPIError as e:
            raise self._ragflow_error(e) from e
        for rf_doc in docs:
            if rf_doc.get("id") == doc.ragflow_document_id:
                doc.pipeline_status = rf_doc.get("run") or doc.pipeline_status or "UNSTART"
                break

        await update_mapping_status(
            self.db, doc, doc.sync_status,
            pipeline_status=doc.pipeline_status,
        )
        return doc

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

    async def _activate_version(self, doc: ExtDocumentMap) -> None:
        await supersede_other_versions(
            self.db, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.source_version_id,
        )
        await set_current_version(
            self.db, doc.tenant_id, doc.source_system,
            doc.external_document_id, doc.source_version_id,
        )
        versions = await get_versions_for_document(
            self.db, doc.tenant_id, doc.source_system, doc.external_document_id,
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
            try:
                await self.ragflow_client.batch_update_status(
                    dataset_id, document_ids, enabled=False,
                )
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e
        await self._set_status(
            doc, "ready", event_status="completed",
            business_status="active", current_version=1,
            pipeline_status=doc.pipeline_status,
        )
        await self._ensure_quality_evaluation(doc)

    async def _ensure_quality_evaluation(self, doc: ExtDocumentMap) -> None:
        from enterprise.gateway.config import config
        from enterprise.gateway.quality.models import get_or_create_evaluation
        from enterprise.gateway.quality.routing import route_document

        routing = route_document(
            media_type=doc.media_type,
            file_name=doc.file_name,
            source_system=doc.source_system,
        )
        try:
            await get_or_create_evaluation(
                self.db,
                tenant_id=doc.tenant_id,
                source_system=doc.source_system,
                external_document_id=doc.external_document_id,
                source_version_id=doc.source_version_id,
                ragflow_dataset_id=doc.ragflow_dataset_id,
                ragflow_document_id=doc.ragflow_document_id,
                routing=routing,
                evaluation_version="1",
                max_attempts=config.quality_max_attempts,
            )
        except Exception:
            # Quality job creation must not roll back or block WP-02 ready.
            logger.exception(
                "Quality evaluation enqueue failed document=%s version=%s",
                doc.external_document_id,
                doc.source_version_id,
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
        except RAGFlowAPIError:
            return doc
        for rf_doc in docs:
            if rf_doc.get("id") != doc.ragflow_document_id:
                continue
            run = rf_doc.get("run") or "UNSTART"
            mapped = map_ragflow_run_to_sync_status(run)
            if mapped == "ready" and doc.sync_status != "ready":
                await self._set_status(
                    doc, "ready", event_status="completed", pipeline_status=run,
                )
                await self._activate_version(doc)
            elif (
                doc.sync_status != mapped
                and transition_allowed(doc.sync_status, mapped, "document")
            ):
                await self._set_status(
                    doc, mapped, event_status="completed", pipeline_status=run,
                )
                if mapped == "failed":
                    await self._ensure_quality_evaluation(doc)
            break
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
            "source": {"bucket": doc.bucket, "objectKey": doc.object_key},
        }
        try:
            source_file = await self.source_adapter.fetch(
                doc.bucket, doc.object_key, doc.sha256,
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
                await self.ragflow_client.update_document_metadata(
                    dataset_id, doc.ragflow_document_id, {},
                )
                await self.ragflow_client.batch_update_status(
                    dataset_id, [doc.ragflow_document_id], True,
                )
                docs = await self.ragflow_client.list_documents(dataset_id)
            except RAGFlowAPIError as e:
                raise self._ragflow_error(e) from e
            for rf_doc in docs:
                if rf_doc.get("id") == doc.ragflow_document_id:
                    doc.pipeline_status = rf_doc.get("run") or doc.pipeline_status or "UNSTART"
                    break
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
        if map_ragflow_run_to_sync_status(doc.pipeline_status) == "ready":
            await self._activate_version(doc)
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
                await self.ragflow_client.batch_update_status(
                    dataset_id, document_ids, enabled,
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
