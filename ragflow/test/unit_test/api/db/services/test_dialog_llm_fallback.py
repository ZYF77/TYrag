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
#  distributed under the Apache License, Version 2.0 (the "License");
#  distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied. See the License for the specific language
#  governing permissions and limitations under the License.
#
"""Unit tests for dialog LLM resolution fallback when tenant_model rows are gone."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_resolve_dialog_llm_config():
    repo_root = Path(__file__).resolve().parents[5]
    source_path = repo_root / "api" / "db" / "services" / "dialog_service.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    fn_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_dialog_llm_config"
    ]
    assert fn_nodes, "_resolve_dialog_llm_config is missing from dialog_service.py"

    known_ids = {"live-llm-id"}
    default_config = {"llm_name": "tenant-default", "model_type": "chat"}
    live_config = {"llm_name": "live-model", "model_type": "chat"}

    def _get_model_config_by_id(_tenant_id, _model_type, model_id):
        if model_id in known_ids:
            return live_config
        raise LookupError(f"TenantModel id={model_id} not found.")

    def _resolve_model_config(_tenant_id, _model_type, model_ref):
        if model_ref in known_ids or model_ref == "live@default@VolcEngine":
            return live_config
        raise LookupError(f"Provider  not found for model {model_ref}.")

    def _resolve_model_type(_tenant_id, model_ref):
        if model_ref in known_ids or model_ref == "live@default@VolcEngine":
            return ["chat"]
        raise LookupError(f"Provider  not found for model {model_ref}.")

    def _get_tenant_default(_tenant_id, _model_type):
        return default_config

    ns = {
        "logging": logging,
        "LLMType": SimpleNamespace(CHAT="chat", VISION="vision"),
        "get_model_config_by_id": _get_model_config_by_id,
        "resolve_model_config": _resolve_model_config,
        "resolve_model_type": _resolve_model_type,
        "get_tenant_default_model_by_type": _get_tenant_default,
    }
    exec(compile(ast.Module(body=fn_nodes, type_ignores=[]), str(source_path), "exec"), ns)
    return ns["_resolve_dialog_llm_config"], live_config, default_config


_resolve_dialog_llm_config, _LIVE, _DEFAULT = _load_resolve_dialog_llm_config()


@pytest.mark.p2
def test_orphaned_tenant_model_id_falls_back_to_tenant_default():
    dialog = SimpleNamespace(
        tenant_id="tenant-1",
        llm_id="ab862526961711f1b378f951ffaa3dfe",
        tenant_llm_id="ab862526961711f1b378f951ffaa3dfe",
    )
    assert _resolve_dialog_llm_config(dialog) == _DEFAULT


@pytest.mark.p2
def test_live_tenant_model_id_is_used():
    dialog = SimpleNamespace(
        tenant_id="tenant-1",
        llm_id="live-llm-id",
        tenant_llm_id="live-llm-id",
    )
    assert _resolve_dialog_llm_config(dialog) == _LIVE
