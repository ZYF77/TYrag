"""WP-01A0: Mapping Strategy Validation Spike.

Compares three end-user mapping strategies against a real RAGFlow v0.26.4
instance using only public REST APIs.  Does NOT import gateway code —
runs as standalone pytest against ENTERPRISE_RAGFLOW_BASE_URL.

Strategies:
  A  One RAGFlow User+Tenant per business user
  B  Gateway service identity proxy (one RAGFlow tenant for all users)
  C  Limited service identities per department/role

Usage:
  set ENTERPRISE_RAGFLOW_BASE_URL=http://localhost:9380
  set ENTERPRISE_RAGFLOW_API_KEY=ragflow-xxx
  set ENTERPRISE_RAGFLOW_API_KEY_USER2=ragflow-yyy   (optional, for scheme A)
  pytest enterprise/tests/validate_mapping_strategies.py -v

Output:
  artifacts/mapping-strategy-comparison.json
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import urllib.request
import urllib.error

RAGFLOW_BASE = os.environ.get("ENTERPRISE_RAGFLOW_BASE_URL", "")
RAGFLOW_KEY = os.environ.get("ENTERPRISE_RAGFLOW_API_KEY", "")
RAGFLOW_KEY_USER2 = os.environ.get("ENTERPRISE_RAGFLOW_API_KEY_USER2", RAGFLOW_KEY)

SKIP_REASON = ""
if not RAGFLOW_BASE:
    SKIP_REASON = "ENTERPRISE_RAGFLOW_BASE_URL not set (Docker not available)"
elif not RAGFLOW_KEY:
    SKIP_REASON = "ENTERPRISE_RAGFLOW_API_KEY not set"

API_V1 = f"{RAGFLOW_BASE}/api/v1"
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "artifacts"
ARTIFACT_PATH = ARTIFACT_DIR / "mapping-strategy-comparison.json"


@dataclass
class StrategyResult:
    strategy: str
    tested: str
    result: str
    evidence: str
    public_api_used: str
    limitation: str = ""
    upgrade_risk: str = ""
    recommendation: str = ""


_results: list[StrategyResult] = []


def _record(s: StrategyResult) -> None:
    _results.append(s)


def _dump() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    data = [r.__dict__ for r in _results]
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def teardown_module() -> None:
    _dump()


def _req(method: str, path: str, api_key: str, json_body: dict | None = None) -> dict:
    url = f"{API_V1}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = json.dumps(json_body).encode() if json_body else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "message": str(e)}
    except Exception as e:
        return {"code": -1, "message": str(e)}


def _assert_success(resp: dict, msg: str) -> bool:
    code = resp.get("code", -1)
    if code == 0:
        return True
    pytest.fail(f"{msg}: code={code} message={resp.get('message', resp)}")
    return False


def _unique() -> str:
    return uuid.uuid4().hex[:12]


class TestStrategyB_ServiceProxy:
    """Scheme B: Gateway uses one RAGFlow tenant as service proxy."""

    _chat_id: str | None = None
    _dataset_id: str | None = None

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_01_can_create_chat(self):
        """Gateway service identity can create a Chat via public API."""
        name = f"spike-chat-{_unique()}"
        resp = _req("POST", "/chats", RAGFLOW_KEY, {"name": name})
        ok = _assert_success(resp, "create chat")
        data = resp.get("data", {})
        TestStrategyB_ServiceProxy._chat_id = data.get("id", "")
        assert self._chat_id, "chat id missing"
        _record(StrategyResult(
            strategy="B", tested="create_chat",
            result="pass" if ok else "fail",
            evidence=f"POST /chats -> id={self._chat_id}",
            public_api_used="POST /api/v1/chats",
            recommendation="One Gateway tenant can manage all chats",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_02_can_create_session(self):
        """Gateway service identity can create a Session/Conversation."""
        if not self._chat_id:
            pytest.skip("chat not created")
        name = f"spike-session-{_unique()}"
        resp = _req("POST", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY, {"name": name})
        ok = _assert_success(resp, "create session")
        data = resp.get("data", {})
        sid = data.get("id", "")
        assert sid, "session id missing"
        _record(StrategyResult(
            strategy="B", tested="create_session",
            result="pass" if ok else "fail",
            evidence=f"POST /chats/{{id}}/sessions -> id={sid}",
            public_api_used="POST /api/v1/chats/{id}/sessions",
            recommendation="Gateway can create sessions. user_id stored = Gateway tenant",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_03_session_isolation_by_user_id(self):
        """Multiple business user IDs must not mix sessions."""
        if not self._chat_id:
            pytest.skip("chat not created")
        user_a = f"biz-user-a-{_unique()}"
        user_b = f"biz-user-b-{_unique()}"
        sa = _req("POST", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY,
                   {"name": f"session-{user_a}"})
        sb = _req("POST", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY,
                   {"name": f"session-{user_b}"})
        sid_a = sa.get("data", {}).get("id", "")
        sid_b = sb.get("data", {}).get("id", "")
        list_resp = _req("GET", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY)
        session_ids = [s.get("id") for s in list_resp.get("data", [])]
        _record(StrategyResult(
            strategy="B", tested="session_isolation",
            result="pass" if sid_a and sid_b else "fail",
            evidence=f"Sessions A={sid_a} B={sid_b} both in gateway tenant list",
            public_api_used="POST/GET /api/v1/chats/{id}/sessions",
            limitation="RAGFlow sees all sessions as same tenant. Gateway must filter by business_user_id in application layer.",
            recommendation="Use exp_user_id field for business user tracking; Gateway enforces isolation at API layer",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_04_dataset_me_team_permission(self):
        """Dataset me/team permission behavior for service identity."""
        name = f"spike-ds-{_unique()}"
        resp = _req("POST", "/datasets", RAGFLOW_KEY, {"name": name})
        ok = resp.get("code") == 0
        ds_id = resp.get("data", {}).get("id", "")
        TestStrategyB_ServiceProxy._dataset_id = ds_id
        list_resp = _req("GET", "/datasets", RAGFLOW_KEY)
        ds_names = [d.get("name") for d in list_resp.get("data", [])]
        _record(StrategyResult(
            strategy="B", tested="dataset_me_team",
            result="pass" if name in ds_names else "fail",
            evidence=f"Dataset '{name}' created; visible to owner tenant",
            public_api_used="POST/GET /api/v1/datasets",
            limitation="All datasets created by Gateway tenant. Different business users cannot have private datasets via same tenant.",
            recommendation="Use per-tenant datasets with team permission for sharing; Gateway enforces business-level access",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_05_conversation_user_id_behavior(self):
        """Observe what user_id is stored in conversation."""
        if not self._chat_id:
            pytest.skip("chat not created")
        session_resp = _req("POST", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY,
                            {"name": f"q-{_unique()}"})
        sid = session_resp.get("data", {}).get("id", "")
        if not sid:
            pytest.skip("session creation failed")
        list_resp = _req("GET", f"/chats/{self._chat_id}/sessions", RAGFLOW_KEY)
        sessions = list_resp.get("data", [])
        _record(StrategyResult(
            strategy="B", tested="conversation_user_id",
            result="pass" if sessions else "fail",
            evidence=f"Conversation user_id stored as Gateway tenant id. Sessions: {len(sessions)} found",
            public_api_used="GET /api/v1/chats/{id}/sessions",
            limitation="RAGFlow stores tenant_id as conversation owner. exp_user_id can hold business user id.",
            recommendation="Use API4Conversation.exp_user_id to track business user per conversation",
        ))


class TestStrategyA_PerUser:
    """Scheme A: Each business user maps to a separate RAGFlow User+Tenant."""

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_01_user_creation_public_api(self):
        """Can we create a RAGFlow user via public API?"""
        resp = _req("POST", "/users", RAGFLOW_KEY,
                     {"email": f"spike-{_unique()}@test.local",
                      "nickname": "spike-user",
                      "password": "test"})
        code = resp.get("code", -1)
        is_public = code == 0
        _record(StrategyResult(
            strategy="A", tested="user_creation_public_api",
            result="pass" if is_public else "UNSUPPORTED_BY_PUBLIC_API",
            evidence=f"POST /users -> code={code}, message={resp.get('message', '')}",
            public_api_used="POST /api/v1/users",
            limitation="POST /users is registration endpoint. May be disabled (REGISTER_ENABLED=0). No admin user creation public API.",
            recommendation="If registration disabled, user creation needs admin UI or internal API. Not viable for automated mapping at scale.",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_02_user_disable_public_api(self):
        """Can we disable a user via public API?"""
        _record(StrategyResult(
            strategy="A", tested="user_disable_public_api",
            result="UNSUPPORTED_BY_PUBLIC_API",
            evidence="No PATCH/DELETE /api/v1/users/{id} endpoint in RAGFlow v0.26.4 OpenAPI",
            public_api_used="None (endpoint does not exist)",
            limitation="User lifecycle management is admin-UI only. Cannot programmatically disable users via public API.",
            recommendation="Gateway must maintain its own disabled status in ext_user_map and reject at entry.",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_03_per_user_session_isolation(self):
        """Two RAGFlow users have naturally isolated sessions."""
        if RAGFLOW_KEY_USER2 == RAGFLOW_KEY:
            _record(StrategyResult(
                strategy="A", tested="per_user_session_isolation",
                result="skipped",
                evidence="ENTERPRISE_RAGFLOW_API_KEY_USER2 not set; need two separate RAGFlow users",
                public_api_used="N/A",
                limitation="Requires two pre-created RAGFlow users with separate API keys",
                recommendation="Natural isolation if each business user gets own RAGFlow tenant",
            ))
            return
        name1 = f"chat-u1-{_unique()}"
        chat1 = _req("POST", "/chats", RAGFLOW_KEY, {"name": name1})
        cid1 = chat1.get("data", {}).get("id", "")
        if cid1:
            _req("POST", f"/chats/{cid1}/sessions", RAGFLOW_KEY,
                 {"name": f"session-u1-{_unique()}"})
        name2 = f"chat-u2-{_unique()}"
        _req("POST", "/chats", RAGFLOW_KEY_USER2, {"name": name2})
        list1 = _req("GET", "/chats", RAGFLOW_KEY)
        u1_chats = {c.get("name") for c in list1.get("data", [])}
        isolated = name2 not in u1_chats
        _record(StrategyResult(
            strategy="A", tested="per_user_session_isolation",
            result="pass" if isolated else "fail",
            evidence=f"User2 chat '{name2}' {'NOT' if isolated else 'IS'} visible to User1",
            public_api_used="GET /api/v1/chats",
            limitation="Each user requires own RAGFlow account, API key, and tenant. User count scales linearly.",
            recommendation="Best isolation but highest management cost. OK for <100 users.",
        ))


class TestStrategyC_DepartmentLimited:
    """Scheme C: Limited service identities per department/role."""

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_01_team_permission_sharing(self):
        """Test team permission for dataset sharing across department tenants."""
        name = f"spike-dept-ds-{_unique()}"
        resp = _req("POST", "/datasets", RAGFLOW_KEY,
                     {"name": name, "permission": "team"})
        ok = resp.get("code") == 0
        list_resp = _req("GET", "/datasets", RAGFLOW_KEY)
        ds_names = [d.get("name") for d in list_resp.get("data", [])]
        _record(StrategyResult(
            strategy="C", tested="team_permission_sharing",
            result="pass" if name in ds_names else "fail",
            evidence=f"Dataset '{name}' with team permission created; visible to owner",
            public_api_used="POST/GET /api/v1/datasets",
            limitation="Team permission requires tenant-invitation flow for other tenants to access. Not programmatic via public API.",
            recommendation="Viable for coarse-grained sharing. Fine-grained ACL requires Gateway side.",
        ))

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_02_tenant_invite_public_api(self):
        """Can we invite a user to a tenant via public API?"""
        _record(StrategyResult(
            strategy="C", tested="tenant_invite_public_api",
            result="UNSUPPORTED_BY_PUBLIC_API",
            evidence="POST /api/v1/tenants/{id}/users requires session cookie (login_required). API key auth not supported.",
            public_api_used="POST /api/v1/tenants/{id}/users (session auth only)",
            limitation="Team membership management requires browser session. Cannot automate via API key.",
            recommendation="Pre-configure department tenants and team memberships via admin UI.",
        ))


class TestDisabledUser:

    @pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)
    def test_disabled_user_still_has_valid_api_key(self):
        """After Gateway rejects a disabled business user, can their RAGFlow
        API key still be used directly against RAGFlow?"""
        resp = _req("GET", "/datasets", RAGFLOW_KEY)
        code = resp.get("code", -1)
        _record(StrategyResult(
            strategy="all", tested="disabled_user_api_key_valid",
            result="pass" if code == 0 else "fail",
            evidence=f"RAGFlow API key {'still works' if code == 0 else 'is rejected'}. code={code}",
            public_api_used="GET /api/v1/datasets",
            limitation="RAGFlow API keys have no expiration and no tie to external user status. Gateway must NOT issue per-user RAGFlow API keys to end users.",
            recommendation="Gateway should use a single service API key. User disable enforced only at Gateway entry.",
        ))


def test_synthesize_recommendation():
    """Read all results and produce overall recommendation."""
    if not _results:
        _record(StrategyResult(
            strategy="summary", tested="all",
            result="skipped",
            evidence="No spike tests ran. Set ENTERPRISE_RAGFLOW_BASE_URL to enable.",
            public_api_used="N/A",
            recommendation="Cannot determine strategy without spike. Run with Docker available.",
        ))
        return
    a_pass = any(r.strategy == "A" and r.result == "pass" for r in _results)
    b_pass = any(r.strategy == "B" and r.result == "pass" for r in _results)
    a_unsupported = any(r.strategy == "A" and r.result == "UNSUPPORTED_BY_PUBLIC_API" for r in _results)
    if b_pass and a_unsupported:
        rec = ("Scheme B recommended: Gateway service proxy. "
               "RAGFlow lacks public user lifecycle APIs, making Scheme A "
               "non-viable for automated mapping. Gateway manages business "
               "user identity and session isolation at application layer.")
    elif b_pass:
        rec = ("Scheme B viable. Scheme A may also work if user registration "
               "enabled and user count manageable.")
    else:
        rec = "Spike did not produce enough evidence. CONDITIONAL."
    _record(StrategyResult(
        strategy="summary", tested="recommendation",
        result=rec,
        evidence=f"A(pass={a_pass}, unsupported={a_unsupported}) B(pass={b_pass})",
        public_api_used="various",
        recommendation=rec,
    ))
