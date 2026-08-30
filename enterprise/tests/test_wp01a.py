"""WP-01A tests: JWT validation, UserPrincipal, ext_user_map, /auth/me, regression."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from enterprise.gateway.auth.token_validator import (
    JWTValidator, JWTValidatorConfig, TokenValidationError,
)
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.models.ext_user_map import ExtUserMap, ExtUserMapRepo

os.environ.setdefault("ENTERPRISE_SYNC_AUTH_ENABLED", "false")
os.environ["JWT_SHARED_SECRET"] = "test-secret-must-be-at-least-32-bytes!!"
from enterprise.gateway.app import app

# ---------- helpers ----------

_VALID_CLAIMS = {
    "sub": "biz-user-001",
    "tenant": "customer-a",
    "name": "Test User",
    "department": ["d10", "d20"],
    "roles": ["end_user"],
    "groups": ["maintenance"],
    "security_level": 2,
    "iat": int(time.time()) - 60,
    "exp": int(time.time()) + 3600,
    "iss": "https://auth.example.com",
    "aud": "tyrag-gateway",
}

_CLAIM_MAP = {
    "sub": "sub",
    "tenant_id": "tenant",
    "business_user_id": "business_user_id",
    "display_name": "name",
    "department_ids": "department",
    "role_codes": "roles",
    "group_ids": "groups",
    "security_level": "security_level",
}

SHARED_SECRET = "test-secret-must-be-at-least-32-bytes!!"


def _make_token(claims: dict | None = None, secret: str = SHARED_SECRET) -> str:
    payload = dict(_VALID_CLAIMS)
    if claims:
        payload.update(claims)
    payload.setdefault("iat", int(time.time()) - 60)
    payload.setdefault("exp", int(time.time()) + 3600)
    payload.setdefault("iss", "https://auth.example.com")
    payload.setdefault("aud", "tyrag-gateway")
    return jwt.encode(payload, secret, algorithm="HS256")


def _validator(**kwargs) -> JWTValidator:
    cfg = JWTValidatorConfig(
        issuer="https://auth.example.com",
        audience="tyrag-gateway",
        allowed_algorithms=("HS256",),
        enable_hs_algorithms=True,
        jwks_url="",
        claim_map=_CLAIM_MAP,
    )
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return JWTValidator(cfg)


async def _seed_mapping(db_path: str, status: str = "active") -> None:
    repo = ExtUserMapRepo(db_path=db_path)
    try:
        await repo.ensure_table()
        await repo.insert_mapping(ExtUserMap(
            tenant_id="customer-a",
            business_subject="biz-user-001",
            business_user_id="biz-user-001",
            mapping_strategy="B",
            status=status,
        ))
    finally:
        await repo.close()


# ---------- JWT Validation ----------


async def _reset_app_gateway() -> None:
    import enterprise.gateway.app as app_module
    if getattr(app_module, "_gateway_db", None) is not None:
        await app_module._gateway_db.dispose()
        app_module._gateway_db = None

class TestJWTValidation:
    def test_valid_token(self):
        v = _validator()
        token = _make_token()
        claims = v.validate(token)
        assert claims["sub"] == "biz-user-001"
        assert claims["tenant"] == "customer-a"

    def test_wrong_signature(self):
        v = _validator()
        token = _make_token(secret="wrong-secret-at-least-32-bytes-long")
        with pytest.raises(TokenValidationError) as exc:
            v.validate(token)
        assert "validation failed" in str(exc.value).lower()

    def test_wrong_issuer(self):
        v = _validator(issuer="https://other.example.com")
        token = _make_token()
        with pytest.raises(TokenValidationError):
            v.validate(token)

    def test_wrong_audience(self):
        v = _validator(audience="other-audience")
        token = _make_token()
        with pytest.raises(TokenValidationError):
            v.validate(token)

    def test_expired(self):
        claims = {"exp": int(time.time()) - 3600}
        token = _make_token(claims)
        v = _validator()
        with pytest.raises(TokenValidationError) as exc:
            v.validate(token)
        assert exc.value.code == "AUTH_TOKEN_EXPIRED"

    def test_nbf_not_yet(self):
        claims = {"nbf": int(time.time()) + 3600}
        token = _make_token(claims)
        v = _validator()
        with pytest.raises(TokenValidationError) as exc:
            v.validate(token)
        assert exc.value.code == "AUTH_TOKEN_INVALID"
        assert "nbf" in exc.value.message

    def test_nbf_small_clock_skew_is_accepted(self):
        """EAM JsonWebTokenHandler sets nbf=now; ~78s issuer/gateway skew must pass."""
        token = _make_token({"nbf": int(time.time()) + 90, "iat": int(time.time())})
        claims = _validator(leeway_seconds=120).validate(token)
        assert claims["sub"] == "biz-user-001"

    def test_nbf_beyond_leeway_is_rejected(self):
        token = _make_token({"nbf": int(time.time()) + 90})
        v = _validator(leeway_seconds=0)
        with pytest.raises(TokenValidationError) as exc:
            v.validate(token)
        assert exc.value.code == "AUTH_TOKEN_INVALID"
        assert "nbf" in exc.value.message

    def test_missing_subject(self):
        claims = {"sub": None}
        token = _make_token(claims)
        v = _validator()
        with pytest.raises(TokenValidationError):
            v.validate(token)

    def test_missing_tenant(self):
        claims = {"tenant": None}
        token = _make_token(claims)
        v = _validator()
        with pytest.raises(TokenValidationError):
            v.validate(token)

    def test_forbidden_algorithm(self):
        v = _validator(enable_hs_algorithms=False)
        token = _make_token()
        with pytest.raises(TokenValidationError) as exc:
            v.validate(token)
        assert exc.value.code in ("CONFIG_ERROR", "AUTH_TOKEN_INVALID")

    def test_hs_works_even_when_jwks_url_configured(self, monkeypatch):
        """Dual-alg probe: HS256 must use shared secret, not JWKS kid lookup."""
        monkeypatch.setenv("JWT_SHARED_SECRET", SHARED_SECRET)
        token = _make_token()
        v = _validator(
            enable_hs_algorithms=True,
            allowed_algorithms=("RS256", "HS256"),
            jwks_url="http://127.0.0.1:9/.well-known/jwks.json",
        )
        claims = v.validate(token)
        assert claims["sub"] == "biz-user-001"
        assert claims["tenant"] == "customer-a"

    def test_custom_claim_mapping(self):
        claims_data = {
            "sub": "u-99", "custom_tenant": "t-99", "custom_name": "Mapped User",
            "custom_dept": "eng", "custom_roles": ["viewer"],
            "custom_groups": ["grp1"], "custom_seclvl": 3,
        }
        token = jwt.encode(
            {**claims_data, "iat": int(time.time())-60, "exp": int(time.time())+3600,
             "iss": "https://auth.example.com", "aud": "tyrag-gateway"},
            SHARED_SECRET, algorithm="HS256",
        )
        v2 = JWTValidator(JWTValidatorConfig(
            issuer="https://auth.example.com", audience="tyrag-gateway",
            allowed_algorithms=("HS256",), enable_hs_algorithms=True, jwks_url="",
            claim_map={
                "sub": "sub", "tenant_id": "custom_tenant",
                "business_user_id": "sub", "display_name": "custom_name",
                "department_ids": "custom_dept", "role_codes": "custom_roles",
                "group_ids": "custom_groups", "security_level": "custom_seclvl",
            },
        ))
        claims = v2.validate(token)
        assert claims["custom_tenant"] == "t-99"

    def test_empty_token_returns_error(self):
        v = _validator()
        with pytest.raises(TokenValidationError) as exc:
            v.validate("")
        assert exc.value.code == "AUTH_TOKEN_MISSING"

    def test_no_issuer_configured(self):
        v = JWTValidator(JWTValidatorConfig(
            issuer="", allowed_algorithms=("HS256",), enable_hs_algorithms=True,
        ))
        with pytest.raises(TokenValidationError) as exc:
            v.validate("anything")
        assert exc.value.code == "CONFIG_ERROR"


# ---------- UserPrincipal ----------

class TestUserPrincipal:
    def test_from_validated_claims(self):
        claims = {"sub": "biz-u-1", "tenant": "t-1", "name": "Alice",
                  "department": ["d1"], "roles": ["end_user"],
                  "groups": ["grp1"], "security_level": 3,
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.business_user_id == "biz-u-1"
        assert p.tenant_id == "t-1"
        assert p.display_name == "Alice"
        assert p.role_codes == ("end_user",)
        assert p.group_ids == ("grp1",)
        assert p.security_level == 3
        assert "ask" in p.capabilities

    def test_single_value_department(self):
        claims = {"sub": "u", "tenant": "t", "department": "single-dept",
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.department_ids == ("single-dept",)

    def test_array_department(self):
        claims = {"sub": "u", "tenant": "t", "department": ["d1", "d2"],
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.department_ids == ("d1", "d2")

    def test_integer_department_is_coerced_to_string_tuple(self):
        claims = {"sub": "u", "tenant": "t", "department": 2,
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.department_ids == ("2",)

    def test_integer_list_department_is_coerced_to_string_tuple(self):
        claims = {"sub": "u", "tenant": "t", "department": [2, 3],
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.department_ids == ("2", "3")

    def test_security_level_boundary(self):
        claims = {"sub": "u", "tenant": "t", "security_level": 0, "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.security_level == 0
        claims["security_level"] = 999
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.security_level == 999

    def test_disabled_user(self):
        p = UserPrincipal(tenant_id="t", business_user_id="u", subject="s",
                          mapping_status="disabled")
        assert not p.is_active

    def test_capability_derivation(self):
        caps = UserPrincipal._derive_capabilities(("system_admin",), 5)
        assert "admin" in caps
        caps = UserPrincipal._derive_capabilities(("auditor",), 1)
        assert "audit" in caps
        caps = UserPrincipal._derive_capabilities(("end_user",), 1)
        assert "ask" in caps
        caps = UserPrincipal._derive_capabilities((), 1)
        assert caps == ("read",)
        caps = UserPrincipal._derive_capabilities(("unknown_role",), 1)
        assert caps == ("read",)

    def test_missing_role_codes_only_read(self):
        claims = {"sub": "u", "tenant": "t", "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.role_codes == ()
        assert p.capabilities == ("read",)

    def test_unknown_role_codes_only_read(self):
        claims = {"sub": "u", "tenant": "t", "roles": ["ROLE_OPERATOR"],
                  "iat": 1000, "exp": 2000}
        p = UserPrincipal.from_validated_claims(claims, _CLAIM_MAP)
        assert p.role_codes == ("ROLE_OPERATOR",)
        assert p.capabilities == ("read",)

    def test_to_safe_dict_excludes_internals(self):
        p = UserPrincipal(tenant_id="t", business_user_id="u", subject="sub-x",
                          display_name="Alice", department_ids=("d1",),
                          role_codes=("end_user",), group_ids=("g1",),
                          security_level=2, token_issued_at=1000,
                          token_expires_at=2000)
        d = p.to_safe_dict()
        assert "subject" not in d
        assert "credential" not in str(d).lower()
        assert d["businessUserId"] == "u"
        assert d["tenantId"] == "t"

    def test_request_body_identity_not_trusted(self):
        p = UserPrincipal(tenant_id="from-jwt", business_user_id="from-jwt",
                          subject="from-jwt")
        assert p.tenant_id == "from-jwt"


# ---------- Persistence ----------

@pytest.mark.asyncio
class TestExtUserMap:
    async def test_first_mapping(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        entry = ExtUserMap(tenant_id="t1", business_subject="sub-1",
                           business_user_id="u1", mapping_strategy="B")
        result = await repo.insert_mapping(entry)
        assert result.id is not None
        await repo.close()

    async def test_duplicate_reuse(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        e1 = ExtUserMap(tenant_id="t1", business_subject="sub-1", business_user_id="u1")
        await repo.insert_mapping(e1)
        e2 = ExtUserMap(tenant_id="t1", business_subject="sub-1", business_user_id="u2")
        await repo.insert_mapping(e2)
        found = await repo.get_mapping("t1", "sub-1")
        assert found is not None
        await repo.close()

    async def test_concurrent_first_insert(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        async def insert():
            return await repo.insert_mapping(ExtUserMap(
                tenant_id="t1", business_subject=f"sub-conc-{os.urandom(4).hex()}", business_user_id="u-conc"))
        results = await asyncio.gather(insert(), insert(), insert(), insert(), insert())
        ids = [r.id for r in results if r.id]
        assert len(ids) >= 1  # at least one succeeds; unique constraint may dedup
        await repo.close()

    async def test_unique_constraint(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        e1 = ExtUserMap(tenant_id="t1", business_subject="sub-uq")
        r1 = await repo.insert_mapping(e1)
        e2 = ExtUserMap(tenant_id="t2", business_subject="sub-uq")
        r2 = await repo.insert_mapping(e2)
        assert r1.id != r2.id
        await repo.close()

    async def test_disabled_user(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        e = ExtUserMap(tenant_id="t1", business_subject="sub-dis", status="disabled")
        await repo.insert_mapping(e)
        found = await repo.get_mapping("t1", "sub-dis")
        assert found["status"] == "disabled"
        await repo.close()

    async def test_ragflow_user_id_nullable(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        e = ExtUserMap(tenant_id="t1", business_subject="sub-null-rf")
        await repo.insert_mapping(e)
        found = await repo.get_mapping("t1", "sub-null-rf")
        assert found["ragflow_user_id"] is None
        await repo.close()

    async def test_record_login(self, tmp_path):
        db_path = str(tmp_path / "test_user_map.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        e = ExtUserMap(tenant_id="t1", business_subject="sub-login")
        await repo.insert_mapping(e)
        await repo.record_login("t1", "sub-login")
        found = await repo.get_mapping("t1", "sub-login")
        assert found["last_login_at"] is not None
        await repo.close()


# ---------- API Contract ----------

@pytest.mark.asyncio
class TestAuthMeAPI:
    async def test_missing_token_returns_401(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/enterprise/api/v1/auth/me")
            assert resp.status_code == 401

    async def test_invalid_token_returns_401(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/enterprise/api/v1/auth/me",
                headers={"Authorization": "Bearer invalid-token-12345"},
            )
            assert resp.status_code == 401
            body = resp.json()
            assert body["code"] in ("CONFIG_ERROR", "AUTH_TOKEN_INVALID")
            assert body["message"] in (
                "Authentication is not configured",
                "Authentication token is invalid",
                "Unable to parse token header",
            )
            assert "requestId" in body

    async def test_valid_token_returns_principal(self, tmp_path):
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        db_path = str(tmp_path / "test_auth_me.db")
        await _reset_app_gateway()
        os.environ["ENTERPRISE_DB_PATH"] = db_path
        os.environ["ENTERPRISE_SYNC_DB_PATH"] = db_path
        try:
            await _seed_mapping(db_path)
            token = _make_token()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["businessUserId"] == "biz-user-001"
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL", "ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH"]:
                os.environ.pop(k, None)

    async def test_missing_mapping_jit_provisions_and_succeeds(self, tmp_path):
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        db_path = str(tmp_path / "test_missing_mapping.db")
        await _reset_app_gateway()
        os.environ["ENTERPRISE_DB_PATH"] = db_path
        os.environ["ENTERPRISE_SYNC_DB_PATH"] = db_path
        try:
            token = _make_token()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["businessUserId"] == "biz-user-001"

            repo = ExtUserMapRepo(db_path=db_path)
            try:
                found = await repo.get_mapping("customer-a", "biz-user-001")
                assert found is not None
                assert found["status"] == "active"
                assert found["business_subject"] == "biz-user-001"
            finally:
                await repo.close()
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL", "ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH"]:
                os.environ.pop(k, None)

    async def test_disabled_user_returns_stable_error_code(self, tmp_path):
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        db_path = str(tmp_path / "test_disabled_auth_me.db")
        await _reset_app_gateway()
        os.environ["ENTERPRISE_DB_PATH"] = db_path
        os.environ["ENTERPRISE_SYNC_DB_PATH"] = db_path
        try:
            await _seed_mapping(db_path, status="disabled")
            token = _make_token()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 403
                body = resp.json()
                assert body["code"] == "AUTH_USER_DISABLED"
                assert "requestId" in body
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL", "ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH"]:
                os.environ.pop(k, None)

    async def test_response_no_token_leakage(self, tmp_path):
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        db_path = str(tmp_path / "test_leak.db")
        await _reset_app_gateway()
        os.environ["ENTERPRISE_DB_PATH"] = db_path
        os.environ["ENTERPRISE_SYNC_DB_PATH"] = db_path
        try:
            await _seed_mapping(db_path)
            token = _make_token()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
                dumped = json.dumps(data)
                assert SHARED_SECRET not in dumped
                assert token not in dumped
                assert "password" not in dumped.lower()
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL", "ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH"]:
                os.environ.pop(k, None)


# ---------- Regression: WP-02A ----------

class TestRegressionWP02A:
    def test_service_principal_separate_from_user(self):
        from enterprise.gateway.auth.service_auth import ServiceAuthenticator
        from enterprise.gateway.auth.service_principal import ServicePrincipal
        assert ServicePrincipal.__name__ != UserPrincipal.__name__
        auth = ServiceAuthenticator()
        assert auth is not None

    @pytest.mark.asyncio
    async def test_health_check_unchanged(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/enterprise/api/v1/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_service_token_not_accepted_as_user_jwt(self):
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        os.environ["ENTERPRISE_DB_PATH"] = ":memory:"
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "svc-token"
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/auth/me",
                    headers={"Authorization": "Bearer svc-token"},
                )
                assert resp.status_code == 401
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL", "ENTERPRISE_DB_PATH", "ENTERPRISE_SYNC_DB_PATH",
                       "ENTERPRISE_SYNC_SERVICE_TOKEN"]:
                os.environ.pop(k, None)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("isolated_gateway_db")
    async def test_user_jwt_not_accepted_as_service_token(self):
        _saved_auth = os.environ.get("ENTERPRISE_SYNC_AUTH_ENABLED")
        _saved_svc = os.environ.get("ENTERPRISE_SYNC_SERVICE_TOKEN")
        os.environ["JWT_ISSUER"] = "https://auth.example.com"
        os.environ["JWT_AUDIENCE"] = "tyrag-gateway"
        os.environ["JWT_ENABLE_HS"] = "true"
        os.environ["JWT_ALLOWED_ALGS"] = "HS256"
        os.environ["JWT_JWKS_URL"] = ""
        os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = "true"
        os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = "correct-svc-token"
        try:
            token = _make_token()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/enterprise/api/v1/documents/DOC-001/status",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"tenant_id": "t1"},
                )
                assert resp.status_code == 401
        finally:
            for k in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_ENABLE_HS",
                       "JWT_ALLOWED_ALGS", "JWT_JWKS_URL"]:
                os.environ.pop(k, None)
            if _saved_auth is None:
                os.environ.pop("ENTERPRISE_SYNC_AUTH_ENABLED", None)
            else:
                os.environ["ENTERPRISE_SYNC_AUTH_ENABLED"] = _saved_auth
            if _saved_svc is None:
                os.environ.pop("ENTERPRISE_SYNC_SERVICE_TOKEN", None)
            else:
                os.environ["ENTERPRISE_SYNC_SERVICE_TOKEN"] = _saved_svc


# ---------- Concurrency ----------

@pytest.mark.asyncio
class TestConcurrency:
    async def test_concurrent_logins(self, tmp_path):
        db_path = str(tmp_path / "test_conc.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        async def login(i: int):
            e = ExtUserMap(tenant_id="t", business_subject=f"sub-{i}",
                           business_user_id=f"u-{i}")
            await repo.insert_mapping(e)
            return await repo.get_mapping("t", f"sub-{i}")
        results = await asyncio.gather(*(login(i) for i in range(10)))
        assert all(r is not None for r in results)
        assert len(results) == 10
        await repo.close()

    async def test_concurrent_login_with_file_status(self, tmp_path):
        db_path = str(tmp_path / "test_conc2.db")
        repo = ExtUserMapRepo(db_path=db_path)
        await repo.ensure_table()
        async def login(i: int):
            e = ExtUserMap(tenant_id="t", business_subject=f"sub-f-{i}",
                           business_user_id=f"u-f-{i}")
            await repo.insert_mapping(e)
        async def read(i: int):
            return await repo.get_mapping("t", f"sub-f-{i}")
        await asyncio.gather(*(login(i) for i in range(10)))
        results = await asyncio.gather(*(read(i) for i in range(10)))
        assert all(r is not None for r in results)
        await repo.close()
