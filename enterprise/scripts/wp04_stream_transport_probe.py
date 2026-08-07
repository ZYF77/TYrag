"""Real-socket stream transport probe for WP-04 Phase 2.

Starts a local HTTP/1.1 server, sends one valid SSE chunk for the chat
completion request, then closes the connection mid-stream. The probe executes
RagflowClient.chat_completion_stream() over a real TCP socket, so the httpx
transport error path (including the logger fix) is proven at integration level
without monkeypatching httpx or the client.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from enterprise.gateway.query.ragflow_client import (  # noqa: E402
    RAGFlowAPIError,
    RAGFlowQueryClient,
)


class StreamFaultServer:
    """Tiny HTTP/1.1 server: valid chat lookup, broken SSE completion stream."""

    def __init__(
        self,
        chat_name: str,
        dataset_ids: list[str] | None = None,
    ) -> None:
        self.chat_name = chat_name
        self.dataset_ids = list(dataset_ids or ["ds-probe"])
        self.port: int | None = None
        self.base_url: str | None = None
        self.chat_lookups = 0
        self.completion_requests = 0
        self.authorization_header = ""
        self.last_completion_body: dict | None = None
        self._server: asyncio.AbstractServer | None = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, path, _ = request_line.decode("latin1").strip().split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin1").partition(":")
                headers[name.strip().lower()] = value.strip()
            content_length = int(headers.get("content-length", "0") or 0)
            if content_length:
                body = await reader.readexactly(content_length)
            else:
                body = b""
            self.authorization_header = headers.get("authorization", "")
            if method == "GET" and path.startswith("/api/v1/chats"):
                self.chat_lookups += 1
                payload = json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "chats": [
                                {
                                    "id": "chat-stream-probe",
                                    "name": self.chat_name,
                                    "dataset_ids": self.dataset_ids,
                                }
                            ]
                        },
                    }
                ).encode("utf-8")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: "
                    + str(len(payload)).encode("ascii")
                    + b"\r\n\r\n"
                    + payload
                )
                await writer.drain()
                return
            if method == "POST" and path.startswith("/api/v1/chat/completions"):
                self.completion_requests += 1
                if body:
                    try:
                        self.last_completion_body = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self.last_completion_body = None
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                chunk = b'data: {"code": 0, "data": {"answer": "partial"}}\n\n'
                writer.write(
                    f"{len(chunk):x}\r\n".encode("ascii")
                    + chunk
                    + b"\r\n"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def __aenter__(self) -> "StreamFaultServer":
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        self._server = None


async def run_direct_probe(api_key: str) -> dict:
    """Run chat_completion_stream against a real mid-stream disconnect."""
    async with StreamFaultServer("enterprise-formal-probe") as server:
        client = RAGFlowQueryClient(
            base_url=server.base_url, api_key=api_key
        )
        client.timeout = 10
        exception_mapped = False
        name_error_observed = False
        sensitive_leaked = False
        try:
            async for _ in client.chat_completion_stream(
                "chat-stream-probe",
                "stream transport probe",
                doc_ids=["doc-probe"],
            ):
                pass
        except RAGFlowAPIError as exc:
            exception_mapped = True
            message = str(exc)
            sensitive_leaked = (
                api_key in message
                or "Authorization" in message
                or "Bearer" in message
            )
        except Exception as exc:
            name_error_observed = "NameError" in type(exc).__name__
            raise
        if server.completion_requests != 1:
            raise AssertionError(
                "probe did not reach chat completion stream: "
                f"{server.completion_requests}"
            )
        if not exception_mapped:
            raise AssertionError(
                "mid-stream disconnect did not map to RAGFlowAPIError"
            )
        if name_error_observed:
            raise AssertionError("NameError observed in transport error path")
        if sensitive_leaked:
            raise AssertionError(
                "transport error leaked API key or authorization header"
            )
        return {
            "streamTransportFailureVerified": True,
            "streamTransportExceptionMapped": True,
            "streamTransportNameErrorObserved": False,
            "streamTransportSensitiveDataLeaked": False,
            "streamTransportServerCompletions": server.completion_requests,
        }


def main() -> int:
    api_key = os.environ.get("RAGFLOW_API_KEY", "probe-api-key")
    evidence = asyncio.run(run_direct_probe(api_key))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
