"""Service principal — represents the calling system identity."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServicePrincipal:
    """Minimal service identity for system-to-system calls.

    Not to be confused with UserPrincipal (future, for end-user auth).
    """
    source_system: str
    authenticated: bool = True
    credential_id: str | None = None
    key_id: str | None = None
    allowed_bindings: frozenset[tuple[str, str]] = field(default_factory=frozenset)
