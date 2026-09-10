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

import pytest

from rag.llm import SupportedLiteLLMProvider
from rag.llm.chat_model import _apply_model_family_policies, _move_litellm_provider_body_fields

pytestmark = pytest.mark.p1


def test_qwen3_uses_system_disabled_default():
    gen_conf, kwargs = _apply_model_family_policies(
        "qwen3-plus",
        backend="base",
        gen_conf={},
        request_kwargs={},
    )

    assert gen_conf == {}
    assert kwargs["extra_body"]["enable_thinking"] is False


def test_qwen3_can_enable_thinking_explicitly():
    gen_conf, kwargs = _apply_model_family_policies(
        "qwen3-plus",
        backend="base",
        gen_conf={"thinking": "enabled", "temperature": 0.2},
        request_kwargs={"extra_body": {"seed": 1}},
    )

    assert gen_conf == {"temperature": 0.2}
    assert kwargs["extra_body"] == {"seed": 1, "enable_thinking": True}


def test_qwen3_preview_variant_forces_thinking_true():
    """qwen3.x-preview models (e.g. qwen3.8-max-preview) only accept enable_thinking=True."""
    gen_conf, kwargs = _apply_model_family_policies(
        "qwen3.8-max-preview",
        backend="base",
        gen_conf={},
        request_kwargs={},
    )

    assert gen_conf == {}
    assert kwargs["extra_body"]["enable_thinking"] is True


def test_qwen3_preview_ignores_disabled_thinking():
    """Even with thinking=disabled, -preview still forces enable_thinking=True."""
    gen_conf, kwargs = _apply_model_family_policies(
        "qwen3.8-max-preview",
        backend="base",
        gen_conf={"thinking": "disabled", "temperature": 0.2},
        request_kwargs={},
    )

    assert "thinking" not in gen_conf
    assert gen_conf == {"temperature": 0.2}
    assert kwargs["extra_body"]["enable_thinking"] is True


@pytest.mark.parametrize(
    "provider",
    [SupportedLiteLLMProvider.Tongyi_Qianwen, SupportedLiteLLMProvider.Dashscope],
)
def test_qwen3_litellm_provider_uses_provider_field(provider):
    gen_conf, kwargs = _apply_model_family_policies(
        "qwen3-max",
        backend="litellm",
        provider=provider,
        gen_conf={"thinking": "disabled"},
        request_kwargs={},
    )

    assert kwargs == {}
    assert gen_conf["enable_thinking"] is False


def test_kimi_thinking_maps_to_moonshot_payload():
    gen_conf, kwargs = _apply_model_family_policies(
        "kimi-k2.6-preview",
        backend="litellm",
        provider=SupportedLiteLLMProvider.Moonshot,
        gen_conf={"thinking": "disabled", "temperature": 0.6},
        request_kwargs={},
    )

    assert kwargs == {}
    assert gen_conf["thinking"] == {"type": "disabled"}
    assert "temperature" not in gen_conf


def test_moonshot_explicit_thinking_does_not_require_exact_kimi_model_name():
    gen_conf, kwargs = _apply_model_family_policies(
        "kimi-latest",
        backend="litellm",
        provider=SupportedLiteLLMProvider.Moonshot,
        gen_conf={"thinking": "disabled"},
        request_kwargs={},
    )

    assert kwargs == {}
    assert gen_conf["thinking"] == {"type": "disabled"}


def test_kimi_keeps_provider_default_when_unspecified():
    gen_conf, kwargs = _apply_model_family_policies(
        "kimi-k2.5-preview",
        backend="litellm",
        provider=SupportedLiteLLMProvider.Moonshot,
        gen_conf={"temperature": 0.6},
        request_kwargs={},
    )

    assert kwargs == {}
    assert "thinking" not in gen_conf
    assert "temperature" not in gen_conf
    assert gen_conf["top_p"] == 0.95
    assert gen_conf["n"] == 1
    assert gen_conf["presence_penalty"] == 0.0
    assert gen_conf["frequency_penalty"] == 0.0


def test_glm_keeps_provider_default_when_unspecified():
    gen_conf, kwargs = _apply_model_family_policies(
        "glm-4.7",
        backend="litellm",
        provider=SupportedLiteLLMProvider.ZHIPU_AI,
        gen_conf={},
        request_kwargs={},
    )

    assert kwargs == {}
    assert gen_conf == {}


def test_glm_thinking_maps_to_zhipu_payload():
    gen_conf, kwargs = _apply_model_family_policies(
        "glm-4.7",
        backend="litellm",
        provider=SupportedLiteLLMProvider.ZHIPU_AI,
        gen_conf={"thinking": "enabled"},
        request_kwargs={},
    )

    assert kwargs == {}
    assert gen_conf["thinking"] == {"type": "enabled"}


def test_litellm_provider_body_fields_move_to_extra_body_before_drop_params():
    completion_args = {
        "model": "kimi-latest",
        "messages": [],
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
    }

    _move_litellm_provider_body_fields(SupportedLiteLLMProvider.Moonshot, completion_args)

    assert completion_args["extra_body"]["thinking"] == {"type": "disabled"}
    assert "thinking" not in completion_args
    assert completion_args["temperature"] == 0.2


def test_litellm_provider_body_fields_preserve_existing_extra_body():
    completion_args = {
        "model": "qwen3-max",
        "messages": [],
        "enable_thinking": False,
        "extra_body": {"seed": 1},
    }

    _move_litellm_provider_body_fields(SupportedLiteLLMProvider.Tongyi_Qianwen, completion_args)

    assert completion_args["extra_body"] == {"seed": 1, "enable_thinking": False}
    assert "enable_thinking" not in completion_args


def test_ark_thinking_maps_to_extra_body_when_enabled():
    """Volc/Base (Ark/Doubao) path: llm_setting.thinking -> extra_body.thinking."""
    gen_conf, kwargs = _apply_model_family_policies(
        "ep-ark-doubao-pro",
        backend="base",
        gen_conf={"thinking": "enabled", "temperature": 0.3},
        request_kwargs={"extra_body": {"seed": 7}},
    )

    assert gen_conf == {"temperature": 0.3}
    assert "thinking" not in gen_conf
    assert kwargs["extra_body"] == {"seed": 7, "thinking": {"type": "enabled"}}


def test_ark_thinking_maps_to_extra_body_when_disabled():
    gen_conf, kwargs = _apply_model_family_policies(
        "doubao-seed-1.6",
        backend="base",
        gen_conf={"thinking": "disabled"},
        request_kwargs={},
    )

    assert gen_conf == {}
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_ark_thinking_bool_maps_to_extra_body():
    gen_conf, kwargs = _apply_model_family_policies(
        "ep-ark-1",
        backend="base",
        gen_conf={"thinking": True},
        request_kwargs={},
    )

    assert "thinking" not in gen_conf
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


def test_ark_keeps_vendor_default_when_thinking_unset():
    gen_conf, kwargs = _apply_model_family_policies(
        "ep-ark-doubao-pro",
        backend="base",
        gen_conf={"temperature": 0.1},
        request_kwargs={},
    )

    assert gen_conf == {"temperature": 0.1}
    assert "extra_body" not in kwargs or "thinking" not in (kwargs.get("extra_body") or {})


def test_base_clean_conf_preserves_thinking_for_policy():
    from rag.llm.chat_model import Base

    class _ConcreteBase(Base):
        pass

    inst = _ConcreteBase.__new__(_ConcreteBase)
    inst.model_name = "ep-ark-1"
    cleaned = inst._clean_conf({"temperature": 0.2, "thinking": "enabled", "model_type": "chat"})
    assert cleaned["thinking"] == "enabled"
    assert cleaned["temperature"] == 0.2
    assert "model_type" not in cleaned
