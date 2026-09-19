"""Focused checks for the enterprise JSON ingestion Parser branch."""

from __future__ import annotations

import sys
import types
from pathlib import Path


def _install_tencentcloud_stubs() -> None:
    """Allow ParserParam import on hosts without the optional TCADP SDK."""

    def ensure(name: str) -> types.ModuleType:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        return module

    ensure("tencentcloud")
    common = ensure("tencentcloud.common")
    credential = ensure("tencentcloud.common.credential")
    credential.Credential = type("Credential", (), {})
    common.credential = credential

    profile = ensure("tencentcloud.common.profile")
    client_profile = ensure("tencentcloud.common.profile.client_profile")
    client_profile.ClientProfile = type("ClientProfile", (), {})
    http_profile = ensure("tencentcloud.common.profile.http_profile")
    http_profile.HttpProfile = type("HttpProfile", (), {})
    profile.client_profile = client_profile
    profile.http_profile = http_profile

    exception = ensure("tencentcloud.common.exception")
    sdk_exc = ensure("tencentcloud.common.exception.tencent_cloud_sdk_exception")
    sdk_exc.TencentCloudSDKException = type("TencentCloudSDKException", (Exception,), {})
    exception.tencent_cloud_sdk_exception = sdk_exc
    common.exception = exception
    common.profile = profile

    lkeap = ensure("tencentcloud.lkeap")
    v202 = ensure("tencentcloud.lkeap.v20240522")
    v202.lkeap_client = ensure("tencentcloud.lkeap.v20240522.lkeap_client")
    v202.models = ensure("tencentcloud.lkeap.v20240522.models")
    lkeap.v20240522 = v202


_install_tencentcloud_stubs()

from rag.flow.parser.parser import ParserParam  # noqa: E402


def test_parser_declares_json_and_jsonl_setup():
    param = ParserParam()
    assert param.setups["json"]["suffix"] == ["json", "jsonl", "ldjson"]
    assert param.setups["json"]["output_format"] == "json"
    assert "json" in param.allowed_output_format


def test_parser_source_wires_json_handler():
    source = Path(__file__).resolve().parents[4] / "rag" / "flow" / "parser" / "parser.py"
    text = source.read_text(encoding="utf-8")
    assert "def _json(self, name, blob, **kwargs):" in text
    assert '"json": self._json' in text
    assert "JsonParser" in text
