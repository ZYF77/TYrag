"""Source file access adapter — stub for WP-02A."""
from dataclasses import dataclass


@dataclass
class SourceFile:
    content: bytes
    file_name: str
    media_type: str
    size: int
    sha256: str | None = None


class SourceAdapter:
    """Downloads files from object storage (stub in WP-02A)."""

    async def fetch(self, bucket: str, object_key: str,
                    expected_sha256: str | None = None) -> SourceFile:
        return SourceFile(
            content=b"stub-file-content",
            file_name=object_key.rsplit("/", 1)[-1] if "/" in object_key else object_key,
            media_type="application/pdf",
            size=len(b"stub-file-content"),
        )


class SourceStub(SourceAdapter):
    """Stub with configurable content for testing."""

    def __init__(self, content: bytes = b"test pdf content"):
        self._content = content

    async def fetch(self, bucket: str, object_key: str,
                    expected_sha256: str | None = None) -> SourceFile:
        return SourceFile(
            content=self._content,
            file_name=object_key,
            media_type="application/pdf",
            size=len(self._content),
            sha256=expected_sha256,
        )
