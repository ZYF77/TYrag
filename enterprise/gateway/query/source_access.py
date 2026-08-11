"""ACL-gated, range-capable access to immutable FILE_SHARE source versions."""

from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from enterprise.gateway.sync.external_source import FileShareSourceAdapter
from enterprise.gateway.sync.models import ExtDocumentMap
from enterprise.gateway.sync.source_adapter import SourceFetchError


class RangeNotSatisfiable(ValueError):
    pass


def _if_range_matches(value: str | None, etag: str, modified_ns: int) -> bool:
    if value is None:
        return True
    if value == etag:
        return True
    try:
        parsed = parsedate_to_datetime(value)
        return modified_ns / 1_000_000_000 <= parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return False


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes="):
        raise RangeNotSatisfiable("unsupported range unit")
    ranges = value[6:].split(",")
    if len(ranges) != 1:
        raise RangeNotSatisfiable("multiple ranges are not supported")
    range_parts = ranges[0].split("-", 1)
    if len(range_parts) != 2:
        raise RangeNotSatisfiable("invalid range")
    start_text, end_text = (part.strip() for part in range_parts)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise RangeNotSatisfiable("invalid suffix range")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or end < start or start >= size:
                raise RangeNotSatisfiable("range is outside source")
            end = min(end, size - 1)
    except (TypeError, ValueError) as exc:
        raise RangeNotSatisfiable("invalid range") from exc
    if size <= 0 or start >= size or end < start:
        raise RangeNotSatisfiable("empty source range")
    return start, end


async def _file_range_chunks(path: Path, start: int, length: int, chunk_size: int = 1024 * 1024):
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        await asyncio.to_thread(handle.seek, start)
        remaining = length
        while remaining:
            chunk = await asyncio.to_thread(handle.read, min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


async def source_response(request: Request, doc: ExtDocumentMap):
    """Return a source response after the caller has enforced citation ACL."""

    if doc.source_kind != "FILE_SHARE" or not doc.storage_root_id or not doc.relative_path:
        return JSONResponse(status_code=404, content={"code": "DOCUMENT_SOURCE_NOT_FOUND"})
    provider = FileShareSourceAdapter()
    try:
        current = provider.stat_source(doc.storage_root_id, doc.relative_path)
        path = provider.resolve_path(doc.storage_root_id, doc.relative_path)
    except SourceFetchError:
        return JSONResponse(status_code=404, content={"code": "DOCUMENT_SOURCE_NOT_FOUND"})
    if (
        doc.source_size is not None and current.size != doc.source_size
    ) or (
        doc.source_modified_ns is not None and current.modified_ns != doc.source_modified_ns
    ):
        return JSONResponse(status_code=409, content={"code": "DOCUMENT_SOURCE_CHANGED"})

    requested_range = request.headers.get("Range")
    if requested_range and not _if_range_matches(
        request.headers.get("If-Range"), current.etag, current.modified_ns
    ):
        requested_range = None
    try:
        selected = parse_single_range(requested_range, current.size)
    except RangeNotSatisfiable:
        return JSONResponse(
            status_code=416,
            content={"code": "RANGE_NOT_SATISFIABLE"},
            headers={"Content-Range": f"bytes */{current.size}"},
        )

    start, end = selected if selected else (0, current.size - 1)
    length = max(0, end - start + 1)
    safe_name = Path(doc.file_name).name.replace('"', "_")
    encoded_name = quote(safe_name, safe="")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": doc.media_type or "application/pdf",
        "Content-Disposition": (
            f'inline; filename="source.pdf"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "ETag": current.etag,
        "Cache-Control": "private, no-store",
    }
    status_code = 200
    if selected:
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{current.size}"
    return StreamingResponse(
        _file_range_chunks(path, start, length),
        status_code=status_code,
        headers=headers,
        media_type=headers["Content-Type"],
    )
