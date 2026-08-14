"""ASGI middleware that audits FILE_SHARE register, inquiry, and live HTTP traffic."""

from __future__ import annotations

import time

from enterprise.gateway.audit_log import (
    _parse_body,
    record_http_event,
    write_feed_register_audit,
    write_inquiry_audit,
)

_REGISTER_PATH = "/enterprise/api/v3/documents"
_INQUIRY_PREFIXES = (
    "/enterprise/api/v2/conversations",
    "/enterprise/api/v2/citations",
    "/enterprise/api/v1/conversations",
    "/enterprise/api/v1/citations",
)
_SKIP_PATHS = {
    "/enterprise/api/v1/diagnostics/http-log",
    "/enterprise/api/v1/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}
_BODY_CAPTURE_LIMIT = 64 * 1024


def _is_register(method: str, path: str) -> bool:
    return method == "POST" and path.rstrip("/") == _REGISTER_PATH


def _is_inquiry(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _INQUIRY_PREFIXES)


def _should_skip(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return path in _SKIP_PATHS or normalized in _SKIP_PATHS


def _header_map(scope) -> dict[str, str]:
    return {
        key.decode("latin1"): value.decode("latin1")
        for key, value in scope.get("headers") or []
    }


def _header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _is_json_content(content_type: str) -> bool:
    lowered = content_type.lower()
    return "application/json" in lowered or lowered.endswith("+json")


def _should_capture_body(headers: dict[str, str]) -> bool:
    content_type = _header(headers, "content-type")
    if not _is_json_content(content_type):
        return False
    length = _header(headers, "content-length")
    if not length:
        return True
    try:
        return int(length) <= _BODY_CAPTURE_LIMIT
    except ValueError:
        return True


def _clip_chunks(chunks: list[bytes]) -> bytes:
    blob = b"".join(chunks)
    return blob[:_BODY_CAPTURE_LIMIT]


def _response_payload(content_type: str, chunks: list[bytes]) -> tuple[object | None, bool]:
    streamed = "text/event-stream" in content_type.lower()
    total = sum(len(part) for part in chunks)
    if streamed:
        return {"_stream": True, "_bytes": total}, True
    if not chunks:
        return None, False
    if not _is_json_content(content_type):
        return {"_bytes": total} if total else None, False
    return _parse_body(_clip_chunks(chunks)), False


class FeedRegisterAuditMiddleware:
    """Kept name for existing imports; audits register + inquiry + live HTTP log."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method") or ""
        path = scope.get("path") or ""
        if _should_skip(path):
            await self.app(scope, receive, send)
            return

        headers = _header_map(scope)
        register = _is_register(method, path)
        inquiry = _is_inquiry(path)
        capture_body = register or inquiry or _should_capture_body(headers)

        queued: list[dict] = []
        chunks: list[bytes] = []
        captured = 0
        if capture_body:
            more = True
            while more:
                message = await receive()
                queued.append(message)
                if message.get("type") == "http.request":
                    part = message.get("body") or b""
                    if captured < _BODY_CAPTURE_LIMIT:
                        take = part[: _BODY_CAPTURE_LIMIT - captured]
                        chunks.append(take)
                        captured += len(take)
                    more = bool(message.get("more_body"))
                else:
                    more = False

            async def replay_receive():
                if queued:
                    return queued.pop(0)
                return await receive()

            inbound_receive = replay_receive
        else:
            inbound_receive = receive

        status_code = 0
        content_type = ""
        response_chunks: list[bytes] = []
        response_captured = 0

        async def send_wrapper(message):
            nonlocal status_code, content_type, response_captured
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
                raw_headers = message.get("headers") or []
                for key, value in raw_headers:
                    if key.decode("latin1").lower() == "content-type":
                        content_type = value.decode("latin1")
            elif message.get("type") == "http.response.body":
                part = message.get("body") or b""
                if response_captured < _BODY_CAPTURE_LIMIT:
                    take = part[: _BODY_CAPTURE_LIMIT - response_captured]
                    response_chunks.append(take)
                    response_captured += len(take)
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, inbound_receive, send_wrapper)
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            query = (scope.get("query_string") or b"").decode("latin1")
            request_body = b"".join(chunks) if capture_body else None
            response_body, streamed = _response_payload(content_type, response_chunks)
            if register:
                write_feed_register_audit(
                    method="POST",
                    path=path or _REGISTER_PATH,
                    headers=headers,
                    body=request_body,
                    http_status=status_code,
                )
            elif inquiry:
                write_inquiry_audit(
                    method=method,
                    path=path,
                    query=query,
                    headers=headers,
                    body=request_body,
                    http_status=status_code,
                    response_body=(
                        {"_stream": True, "_bytes": sum(len(part) for part in response_chunks)}
                        if streamed
                        else b"".join(response_chunks)
                    ),
                    streamed=streamed,
                )
            record_http_event(
                {
                    "kind": (
                        "feed.register.inbound"
                        if register
                        else "inquiry.http"
                        if inquiry
                        else "http"
                    ),
                    "direction": "inbound",
                    "method": method,
                    "path": path,
                    "query": query,
                    "http_status": status_code,
                    "duration_ms": duration_ms,
                    "body": _parse_body(request_body) if request_body else None,
                    "response_body": response_body,
                    "streamed": streamed,
                }
            )
