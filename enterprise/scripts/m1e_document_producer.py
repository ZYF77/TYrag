"""Server-side HMAC producer for the M1-E document integration boundary.

The browser Harness owns only the user JWT session and the real SSE flow. This
module is the small service/CLI side that signs document requests with the
existing frozen service-auth algorithm.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from enterprise.gateway.auth.service_auth import sign_request


JsonObject = dict[str, Any]
Opener = Callable[..., Any]


class ProducerConfigurationError(ValueError):
    """Raised when a server-side producer setting is absent or invalid."""


class ProducerRequestError(RuntimeError):
    """A request failed without retaining or printing the response payload."""

    def __init__(self, status: int | None, code: str):
        self.status = status
        self.code = code
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"producer request failed: {code}{suffix}")


@dataclass(frozen=True)
class ProducerConfig:
    base_url: str
    key_id: str
    secret: str = field(repr=False)
    tenant_id: str
    source_system: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProducerConfigurationError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ProducerConfigurationError("base_url must not include query or fragment")
        if not self.key_id.strip():
            raise ProducerConfigurationError("key_id is required")
        if not self.secret:
            raise ProducerConfigurationError("secret is required")
        if not self.tenant_id.strip() or not self.source_system.strip():
            raise ProducerConfigurationError("tenant_id and source_system are required")
        if self.timeout_seconds <= 0:
            raise ProducerConfigurationError("timeout_seconds must be positive")
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        object.__setattr__(self, "key_id", self.key_id.strip())
        object.__setattr__(self, "tenant_id", self.tenant_id.strip())
        object.__setattr__(self, "source_system", self.source_system.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProducerConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise ProducerConfigurationError(f"{name} is required")
            return value

        timeout_text = values.get("M1E_PRODUCER_TIMEOUT_SECONDS", "15").strip()
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ProducerConfigurationError(
                "M1E_PRODUCER_TIMEOUT_SECONDS must be numeric"
            ) from exc
        return cls(
            base_url=required("M1E_PRODUCER_BASE_URL"),
            key_id=required("M1E_PRODUCER_KEY_ID"),
            secret=required("M1E_PRODUCER_SECRET"),
            tenant_id=required("M1E_PRODUCER_TENANT_ID"),
            source_system=required("M1E_PRODUCER_SOURCE_SYSTEM"),
            timeout_seconds=timeout,
        )


class DocumentProducer:
    """Sign and send the M1-E v2/v3 document operations needed by the Harness."""

    def __init__(self, config: ProducerConfig):
        self.config = config

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        timestamp: str | None = None,
        opener: Opener | None = None,
        request_base_url: str | None = None,
    ) -> JsonObject:
        timestamp = str(int(time.time())) if timestamp is None else timestamp
        if len(timestamp) != 10 or not timestamp.isdigit():
            raise ValueError("timestamp must be a ten-digit epoch value")
        target = f"{request_base_url or self.config.base_url}{path}"
        if query:
            target = f"{target}?{query}"
        parsed = urlsplit(target)
        signature = sign_request(
            secret=self.config.secret,
            timestamp=timestamp,
            method=method,
            path=parsed.path,
            query=parsed.query,
            body=body,
        )
        request = Request(
            target,
            data=body or None,
            method=method.upper(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-TY-Timestamp": timestamp,
                "X-TY-Key-Id": self.config.key_id,
                "X-TY-Signature": signature,
            },
        )
        transport = urlopen if opener is None else opener
        try:
            with transport(request, timeout=self.config.timeout_seconds) as response:
                status = int(response.getcode() or 200)
                raw = response.read()
        except HTTPError as exc:
            raise ProducerRequestError(exc.code, self._response_code(exc)) from None
        except (OSError, URLError) as exc:
            del exc
            raise ProducerRequestError(None, "PRODUCER_NETWORK_ERROR") from None
        if status >= 400:
            raise ProducerRequestError(status, self._json_code(raw))
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProducerRequestError(status, "PRODUCER_INVALID_RESPONSE") from exc
        if not isinstance(value, dict):
            raise ProducerRequestError(status, "PRODUCER_INVALID_RESPONSE")
        return value

    @staticmethod
    def _json_code(raw: bytes) -> str:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "HTTP_ERROR"
        return value.get("code", "HTTP_ERROR") if isinstance(value, dict) else "HTTP_ERROR"

    def _response_code(self, response: HTTPError) -> str:
        try:
            return self._json_code(response.read())
        except OSError:
            return "HTTP_ERROR"

    def submit_document(
        self,
        command: Mapping[str, Any],
        *,
        timestamp: str | None = None,
        opener: Opener | None = None,
    ) -> JsonObject:
        if not isinstance(command, Mapping):
            raise ValueError("document command must be an object")
        for field_name, expected in (
            ("tenantId", self.config.tenant_id),
            ("sourceSystem", self.config.source_system),
        ):
            if command.get(field_name) != expected:
                raise ValueError(f"document command {field_name} must match producer binding")
        body = json.dumps(
            dict(command), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return self._request(
            "POST",
            "/documents",
            body=body,
            timestamp=timestamp,
            opener=opener,
        )

    def get_document_status(
        self,
        external_document_id: str,
        *,
        source_version_id: str | None = None,
        timestamp: str | None = None,
        opener: Opener | None = None,
    ) -> JsonObject:
        if not external_document_id.strip():
            raise ValueError("external_document_id is required")
        query_values = [
            ("tenantId", self.config.tenant_id),
            ("sourceSystem", self.config.source_system),
        ]
        if source_version_id:
            query_values.append(("sourceVersionId", source_version_id))
        query = urlencode(query_values, quote_via=quote, safe="-._~")
        path = f"/documents/{quote(external_document_id, safe='-._~')}/status"
        return self._request(
            "GET",
            path,
            query=query,
            timestamp=timestamp,
            opener=opener,
        )

    def get_status_url(
        self,
        status_url: str,
        *,
        timestamp: str | None = None,
        opener: Opener | None = None,
    ) -> JsonObject:
        """Poll the exact relative ``statusUrl`` returned by FILE_SHARE v3.

        v3 owns the path and query encoding in this URL.  Reconstructing it
        from an external document ID can change the signed target, so this
        method deliberately accepts only the server-provided relative URL.
        """
        if not isinstance(status_url, str) or not status_url.strip():
            raise ValueError("status_url is required")
        parsed = urlsplit(status_url.strip())
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not parsed.path.startswith("/enterprise/api/v3/documents/")
            or not parsed.path.endswith("/status")
            or not parsed.query
        ):
            raise ValueError("status_url must be a server-provided v3 relative URL")
        gateway = urlsplit(self.config.base_url)
        origin = f"{gateway.scheme}://{gateway.netloc}"
        return self._request(
            "GET",
            parsed.path,
            query=parsed.query,
            timestamp=timestamp,
            opener=opener,
            request_base_url=origin,
        )


def _summary(value: Mapping[str, Any]) -> JsonObject:
    fields = (
        "operationId",
        "externalDocumentId",
        "sourceVersionId",
        "deduplicated",
        "statusUrl",
        "status",
        "stage",
        "pipelineStatus",
        "parseCompleted",
        "indexCompleted",
        "retrievable",
        "qualityStatus",
        "errorCode",
        "eventStatus",
        "updatedAt",
    )
    return {key: value[key] for key in fields if key in value}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M1-E server-side document producer")
    commands = parser.add_subparsers(dest="operation", required=True)
    submit = commands.add_parser("submit", help="sign and POST one document command")
    submit.add_argument("--payload-file", required=True, type=Path)
    status = commands.add_parser("status", help="sign and GET one document status")
    status_target = status.add_mutually_exclusive_group(required=True)
    status_target.add_argument("--external-document-id")
    status_target.add_argument(
        "--status-url",
        help="exact relative statusUrl returned by FILE_SHARE v3 registration",
    )
    status.add_argument("--source-version-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        producer = DocumentProducer(ProducerConfig.from_env())
        if args.operation == "submit":
            command = json.loads(args.payload_file.read_text(encoding="utf-8"))
            if not isinstance(command, dict):
                raise ValueError("payload file must contain a JSON object")
            result = producer.submit_document(command)
        elif args.status_url:
            result = producer.get_status_url(args.status_url)
        else:
            result = producer.get_document_status(
                args.external_document_id,
                source_version_id=args.source_version_id,
            )
        print(json.dumps(_summary(result), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ProducerConfigurationError, ProducerRequestError, ValueError) as exc:
        code = exc.code if isinstance(exc, ProducerRequestError) else "PRODUCER_CONFIGURATION_ERROR"
        print(f"m1e producer error: {code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
