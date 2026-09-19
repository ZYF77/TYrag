#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
"""P0: Memory add_message trusts client subject only for AUTH_API (API Key)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class _PassthroughManager:
    def route(self, *_args, **_kwargs):
        return lambda func: func


def _stub(monkeypatch, name, **attrs):
    mod = ModuleType(name)
    # Mark as package so "from api.apps.services import X" works.
    if name.count(".") <= 2:
        mod.__path__ = []  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _load_memory_api(monkeypatch, *, auth_type, auth_via_api_token=False, add_calls=None):
    add_calls = add_calls if add_calls is not None else []

    class _G:
        pass

    g = _G()
    g.auth_type = auth_type
    g.auth_via_api_token = auth_via_api_token

    async def _add_message(memory_ids, message_dict):
        add_calls.append({"memory_ids": memory_ids, "message_dict": dict(message_dict)})
        return True, "ok"

    async def _get_request_json():
        return {
            "memory_id": ["mem-1"],
            "agent_id": "chat:abc",
            "session_id": "sess-1",
            "user_input": "hello",
            "agent_response": "world",
            "user_id": "client-spoofed-subject",
        }

    memory_api_service = SimpleNamespace(
        add_message=_add_message,
        get_memory_messages=lambda *_a, **_k: None,
        forget_message=lambda *_a, **_k: None,
        update_message_status=lambda *_a, **_k: None,
        search_message=lambda *_a, **_k: [],
        get_messages=lambda *_a, **_k: [],
        get_message_content=lambda *_a, **_k: None,
        create_memory=lambda *_a, **_k: None,
    )

    _stub(
        monkeypatch,
        "api.apps",
        AUTH_API="API",
        AUTH_JWT="JWT",
        current_user=SimpleNamespace(id="owner-tenant-id"),
        login_required=lambda func=None, **_kwargs: (lambda f: f) if func is None else func,
    )
    _stub(monkeypatch, "api.apps.services", memory_api_service=memory_api_service)
    monkeypatch.setitem(sys.modules, "api.apps.services.memory_api_service", memory_api_service)

    _stub(
        monkeypatch,
        "api.utils.api_utils",
        validate_request=lambda *_a, **_k: lambda func: func,
        get_request_json=_get_request_json,
        get_error_argument_result=lambda message="bad": {"code": 102, "message": message},
        get_json_result=lambda code=0, message="", data=None: {
            "code": code,
            "message": message,
            "data": data,
        },
    )
    _stub(
        monkeypatch,
        "api.utils.pagination_utils",
        DEFAULT_PAGE=1,
        DEFAULT_PAGE_SIZE=30,
        validate_rest_api_page=lambda v: v,
        validate_rest_api_page_size=lambda v: v,
    )
    _stub(
        monkeypatch,
        "api.db.joint_services.tenant_model_service",
        ensure_tenant_model_ids_for_params=lambda *_a, **_k: None,
    )
    _stub(
        monkeypatch,
        "common.constants",
        RetCode=SimpleNamespace(
            SUCCESS=0,
            SERVER_ERROR=500,
            NOT_FOUND=404,
            ARGUMENT_ERROR=102,
            AUTHENTICATION_ERROR=401,
        ),
    )
    _stub(
        monkeypatch,
        "common.exceptions",
        ArgumentException=Exception,
        NotFoundException=Exception,
    )
    _stub(monkeypatch, "quart", request=SimpleNamespace(args={}), g=g)

    # Ensure parent packages exist for nested stubs.
    if "api" not in sys.modules:
        _stub(monkeypatch, "api")
    if "api.utils" not in sys.modules:
        _stub(monkeypatch, "api.utils")
    if "api.db" not in sys.modules:
        _stub(monkeypatch, "api.db")
    if "api.db.joint_services" not in sys.modules:
        _stub(monkeypatch, "api.db.joint_services")
    if "common" not in sys.modules:
        _stub(monkeypatch, "common")

    repo_root = Path(__file__).resolve().parents[5]
    module_path = repo_root / "api" / "apps" / "restful_apis" / "memory_api.py"
    spec = importlib.util.spec_from_file_location(
        "test_memory_add_message_subject_mod", module_path
    )
    module = importlib.util.module_from_spec(spec)
    module.manager = _PassthroughManager()
    monkeypatch.setitem(sys.modules, "test_memory_add_message_subject_mod", module)
    spec.loader.exec_module(module)
    return module, add_calls


@pytest.mark.p0
class TestMemoryAddMessageSubject:
    def test_api_key_auth_trusts_client_user_id(self, monkeypatch):
        module, calls = _load_memory_api(monkeypatch, auth_type="API")
        asyncio.run(module.add_message())
        assert calls[0]["message_dict"]["user_id"] == "client-spoofed-subject"

    def test_jwt_auth_ignores_client_user_id(self, monkeypatch):
        module, calls = _load_memory_api(monkeypatch, auth_type="JWT")
        asyncio.run(module.add_message())
        assert calls[0]["message_dict"]["user_id"] == "owner-tenant-id"

    def test_compat_auth_via_api_token_flag(self, monkeypatch):
        module, calls = _load_memory_api(
            monkeypatch, auth_type=None, auth_via_api_token=True
        )
        asyncio.run(module.add_message())
        assert calls[0]["message_dict"]["user_id"] == "client-spoofed-subject"
