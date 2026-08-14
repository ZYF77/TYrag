#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Unit tests for chat model-id pair sync.

Extracts `_normalize_model_pair` and `_tenant_llm_id_for_override` from
`chat_api.py` via AST so the tests do not import the full API module.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest


def _load_chat_api_helpers():
    repo_root = Path(__file__).resolve().parents[5]
    source_path = repo_root / "api" / "apps" / "restful_apis" / "chat_api.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = {"_normalize_model_pair", "_tenant_llm_id_for_override"}
    fn_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted
    ]
    assert {node.name for node in fn_nodes} == wanted, {node.name for node in fn_nodes}

    known_ids = {"old-llm-id", "doubao-llm-id"}
    composites = {
        "old-llm-id": "old@default@VolcEngine",
        "doubao-llm-id": "doubao@default@VolcEngine",
    }

    def _get_model_config_by_id(_tenant_id, _model_type, model_ref):
        if model_ref in known_ids:
            return {}
        raise LookupError(f"unknown tenant model id: {model_ref}")

    def _resolve_model_id(_tenant_id, _model_type, model_name):
        if model_name == "doubao@default@VolcEngine":
            return "doubao-llm-id"
        raise LookupError(f"unknown model name: {model_name}")

    def _get_composite(model_id):
        if model_id in composites:
            return composites[model_id]
        raise LookupError(f"no composite for {model_id}")

    async def _thread_pool_exec(func, *args, **kwargs):
        return func(*args, **kwargs)

    ns = {
        "logging": logging,
        "thread_pool_exec": _thread_pool_exec,
        "get_model_config_by_id": _get_model_config_by_id,
        "resolve_model_id": _resolve_model_id,
        "get_composite_model_name_by_id": _get_composite,
        "_DEFAULT_RERANK_MODELS": set(),
    }
    exec(compile(ast.Module(body=fn_nodes, type_ignores=[]), str(source_path), "exec"), ns)
    return ns["_normalize_model_pair"], ns["_tenant_llm_id_for_override"]


_normalize_model_pair, _tenant_llm_id_for_override = _load_chat_api_helpers()


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.p2
def test_normalize_model_pair_syncs_stale_tenant_id_when_llm_id_is_tenant_model_id():
    req = {"llm_id": "doubao-llm-id", "tenant_llm_id": "old-llm-id"}
    err = _run(_normalize_model_pair(req, "tenant-1", "llm_id", "tenant_llm_id", "chat"))
    assert err is None, err
    assert req["llm_id"] == "doubao@default@VolcEngine"
    assert req["tenant_llm_id"] == "doubao-llm-id"


@pytest.mark.p2
def test_normalize_model_pair_ignores_orphaned_tenant_id_when_llm_id_is_valid():
    """Re-imported models leave dialog.tenant_llm_id pointing at a deleted row.
    Apply must take the newly selected llm_id instead of rejecting the stale id."""
    req = {"llm_id": "doubao-llm-id", "tenant_llm_id": "orphaned-id"}
    err = _run(_normalize_model_pair(req, "tenant-1", "llm_id", "tenant_llm_id", "chat"))
    assert err is None, err
    assert req["llm_id"] == "doubao@default@VolcEngine"
    assert req["tenant_llm_id"] == "doubao-llm-id"


@pytest.mark.p2
def test_normalize_model_pair_ignores_orphaned_tenant_id_when_llm_id_is_composite():
    req = {"llm_id": "doubao@default@VolcEngine", "tenant_llm_id": "orphaned-id"}
    err = _run(_normalize_model_pair(req, "tenant-1", "llm_id", "tenant_llm_id", "chat"))
    assert err is None, err
    assert req["llm_id"] == "doubao@default@VolcEngine"
    assert req["tenant_llm_id"] == "doubao-llm-id"


@pytest.mark.p2
def test_normalize_model_pair_still_rejects_composite_name_mismatch():
    req = {"llm_id": "doubao@default@VolcEngine", "tenant_llm_id": "old-llm-id"}
    err = _run(_normalize_model_pair(req, "tenant-1", "llm_id", "tenant_llm_id", "chat"))
    assert err == "`llm_id` and `tenant_llm_id` must refer to the same model"


@pytest.mark.p2
def test_tenant_llm_id_for_override_uses_request_model_id():
    resolved = _run(_tenant_llm_id_for_override("tenant-1", "doubao-llm-id", "chat"))
    assert resolved == "doubao-llm-id"
