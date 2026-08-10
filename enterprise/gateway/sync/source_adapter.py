"""Source file access adapters for MinIO/S3 and deterministic test stubs."""
import asyncio
import hashlib
import os
from dataclasses import dataclass

from enterprise.gateway.config import config


class SourceFetchError(Exception):
    """Base error for source object retrieval failures."""


class SourceHashMismatch(SourceFetchError):
    """Downloaded object does not match the expected SHA256."""


class SourceTooLarge(SourceFetchError):
    """Downloaded object exceeds the configured size limit."""


class SourceAdapterUnavailable(SourceFetchError):
    """S3 client dependency or configuration is not available."""


class SourceStorageError(SourceFetchError):
    """An object storage write or delete operation failed."""


@dataclass
class SourceFile:
    content: bytes
    file_name: str
    media_type: str
    size: int
    sha256: str | None = None


class SourceAdapter:
    """Base adapter contract. Implementations must be safe for async use."""

    async def fetch(
        self,
        bucket: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> SourceFile:
        raise NotImplementedError


class S3SourceAdapter(SourceAdapter):
    """Downloads business archive objects from an S3-compatible endpoint."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region_name: str | None = None,
        max_size_bytes: int | None = None,
        path_style: bool | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT", "")
        self.access_key = access_key or os.getenv("S3_ACCESS_KEY", "")
        self.secret_key = secret_key or os.getenv("S3_SECRET_KEY", "")
        self.region_name = region_name or os.getenv("S3_REGION", "")
        self.max_size_bytes = max_size_bytes or int(
            os.getenv("S3_MAX_SIZE_MB", "512")
        ) * 1024 * 1024
        self.path_style = (
            path_style
            if path_style is not None
            else os.getenv("S3_PATH_STYLE", "true").lower() == "true"
        )

    def _client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise SourceAdapterUnavailable(
                "boto3 is required for S3 source adapter"
            ) from e
        if not self.endpoint_url or not self.access_key or not self.secret_key:
            raise SourceAdapterUnavailable("S3 endpoint or credentials are not configured")
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region_name or "us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if self.path_style else "auto"},
            ),
        )

    def _download(self, bucket: str, object_key: str) -> SourceFile:
        client = self._client()
        try:
            response = client.get_object(Bucket=bucket, Key=object_key)
        except Exception as e:
            raise SourceFetchError(f"Failed to read object {bucket}/{object_key}") from e
        content = response["Body"].read(self.max_size_bytes + 1)
        if len(content) > self.max_size_bytes:
            raise SourceTooLarge(
                f"Object exceeds {self.max_size_bytes} byte size limit"
            )
        media_type = response.get("ContentType") or "application/octet-stream"
        return SourceFile(
            content=content,
            file_name=object_key.rsplit("/", 1)[-1] if "/" in object_key else object_key,
            media_type=media_type,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def fetch(
        self,
        bucket: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> SourceFile:
        source_file = await asyncio.to_thread(self._download, bucket, object_key)
        if (
            expected_sha256
            and source_file.sha256
            and source_file.sha256.lower() != expected_sha256.lower()
        ):
            raise SourceHashMismatch(
                f"SHA256 mismatch for {bucket}/{object_key}"
            )
        return source_file

    def _put_object(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        media_type: str,
    ) -> None:
        client = self._client()
        try:
            client.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=content,
                ContentType=media_type,
            )
        except Exception as e:
            raise SourceStorageError(
                f"Failed to write object {bucket}/{object_key}"
            ) from e

    async def put_object(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        media_type: str,
    ) -> None:
        """Write an object using the same credential boundary as source reads."""
        if len(content) > self.max_size_bytes:
            raise SourceTooLarge(
                f"Object exceeds {self.max_size_bytes} byte size limit"
            )
        await asyncio.to_thread(
            self._put_object, bucket, object_key, content, media_type
        )

    def _delete_object(self, bucket: str, object_key: str) -> None:
        client = self._client()
        try:
            client.delete_object(Bucket=bucket, Key=object_key)
        except Exception as e:
            raise SourceStorageError(
                f"Failed to delete object {bucket}/{object_key}"
            ) from e

    async def delete_object(self, bucket: str, object_key: str) -> None:
        """Delete an object without exposing the storage client to callers."""
        await asyncio.to_thread(self._delete_object, bucket, object_key)


class SourceStub(SourceAdapter):
    """Configurable in-memory source for tests."""

    def __init__(
        self,
        content: bytes = b"test pdf content",
        sha256: str | None = None,
    ) -> None:
        self._content = content
        self._sha256 = sha256 or hashlib.sha256(content).hexdigest()

    async def fetch(
        self,
        bucket: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> SourceFile:
        actual = hashlib.sha256(self._content).hexdigest()
        if expected_sha256 and actual.lower() != expected_sha256.lower():
            raise SourceHashMismatch(
                f"SHA256 mismatch for {bucket}/{object_key}"
            )
        return SourceFile(
            content=self._content,
            file_name=object_key,
            media_type="application/pdf",
            size=len(self._content),
            sha256=actual,
        )
