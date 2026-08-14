"""UserPrincipal — represents an authenticated end user.

Not to be confused with ServicePrincipal (system-to-system auth for WP-02A).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UserPrincipal:
    """Authenticated business end-user identity.

    Populated from verified JWT claims — never from request body.
    """
    tenant_id: str
    business_user_id: str
    subject: str
    display_name: str = ""
    department_ids: tuple[str, ...] = ()
    role_codes: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()
    security_level: int = 0
    token_issued_at: int = 0
    token_expires_at: int = 0
    mapping_status: str = "active"
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_validated_claims(
        cls,
        claims: dict[str, Any],
        claim_map: dict[str, str],
        mapping_status: str = "active",
    ) -> UserPrincipal:
        """Build UserPrincipal from verified JWT claims using configurable mapping.

        claim_map keys: sub, tenant_id, business_user_id, display_name,
                         department_ids, role_codes, group_ids, security_level
        """
        def _claim(key: str, default: Any = "") -> Any:
            return claims.get(claim_map.get(key, key), default)

        def _claim_list(key: str) -> tuple[str, ...]:
            value = _claim(key, [])
            if isinstance(value, bool):
                return ()
            if isinstance(value, str):
                return (value,) if value else ()
            if isinstance(value, int):
                return (str(value),)
            if isinstance(value, list):
                return tuple(str(v) for v in value)
            return ()

        def _claim_int(key: str) -> int:
            try:
                return int(_claim(key, 0))
            except (TypeError, ValueError):
                return 0

        sub = str(_claim("sub", ""))
        tenant_id = str(_claim("tenant_id", ""))
        business_user_id = str(_claim("business_user_id", sub))
        display_name = str(_claim("display_name", ""))
        department_ids = _claim_list("department_ids")
        role_codes = _claim_list("role_codes")
        group_ids = _claim_list("group_ids")
        security_level = _claim_int("security_level")

        iat = int(claims.get("iat", 0))
        exp = int(claims.get("exp", 0))

        capabilities = cls._derive_capabilities(role_codes, security_level)

        return cls(
            tenant_id=tenant_id,
            business_user_id=business_user_id or sub,
            subject=sub,
            display_name=display_name,
            department_ids=department_ids,
            role_codes=role_codes,
            group_ids=group_ids,
            security_level=security_level,
            token_issued_at=iat,
            token_expires_at=exp,
            mapping_status=mapping_status,
            capabilities=capabilities,
        )

    @staticmethod
    def _derive_capabilities(
        role_codes: tuple[str, ...],
        security_level: int,
    ) -> tuple[str, ...]:
        caps: set[str] = {"read"}
        if "end_user" in role_codes:
            caps.update({"ask", "list_sessions", "view_citations"})
        if "knowledge_maintainer" in role_codes:
            caps.update({"upload", "manage_metadata", "review"})
        if "system_admin" in role_codes:
            caps.update({"admin"})
        if "auditor" in role_codes:
            caps.add("audit")
        return tuple(sorted(caps))

    @property
    def is_expired(self) -> bool:
        now = int(time.time())
        if self.token_expires_at and now >= self.token_expires_at:
            return True
        return False

    @property
    def is_active(self) -> bool:
        return self.mapping_status == "active" and not self.is_expired

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dict safe for API responses.

        Never includes raw token, internal PKs, or credential material.
        """
        return {
            "businessUserId": self.business_user_id,
            "displayName": self.display_name,
            "tenantId": self.tenant_id,
            "departmentIds": list(self.department_ids),
            "roles": list(self.role_codes),
            "capabilities": list(self.capabilities),
            "securityLevel": self.security_level,
            "mappingStatus": self.mapping_status,
        }
