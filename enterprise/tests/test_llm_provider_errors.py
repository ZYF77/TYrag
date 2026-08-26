"""Unit tests for LiteLLM / Dashscope → Gateway error classification."""

from __future__ import annotations

from enterprise.gateway.app import SAFE_ERROR_MESSAGES
from enterprise.gateway.query.llm_provider_errors import classify_llm_provider_error

MODALITY_SAMPLE = (
    '**ERROR**: INVALID_REQUEST - litellm.BadRequestError: DashscopeException - '
    'data: {"error":{"code":"invalid_parameter_error","param":null,'
    '"message":"The provided messages input is invalid. The error info is '
    '[Unexpected item type in content.]","type":"invalid_request_error"}} '
    "LiteLLM Retried: 5 times"
)

BILLING_SAMPLE = (
    "**ERROR**: litellm.BadRequestError: DashscopeException - "
    "Arrearage / Access denied, please make sure your account has enough quota"
)

GENERIC_REJECT_SAMPLE = (
    "**ERROR**: INVALID_REQUEST - litellm.BadRequestError: DashscopeException - "
    'data: {"error":{"code":"invalid_parameter_error","message":"bad args"}}'
)


def test_classify_modality_unsupported():
    result = classify_llm_provider_error(MODALITY_SAMPLE)
    assert result == ("LLM_MODALITY_UNSUPPORTED", 502)
    assert "图片" in SAFE_ERROR_MESSAGES["LLM_MODALITY_UNSUPPORTED"]


def test_classify_billing_arrearage():
    result = classify_llm_provider_error(BILLING_SAMPLE)
    assert result == ("LLM_PROVIDER_BILLING", 502)
    assert "欠费" in SAFE_ERROR_MESSAGES["LLM_PROVIDER_BILLING"]


def test_classify_billing_chinese_arrearage():
    result = classify_llm_provider_error("模型调用失败：账号欠费，请充值后重试")
    assert result == ("LLM_PROVIDER_BILLING", 502)


def test_classify_generic_provider_rejected():
    result = classify_llm_provider_error(GENERIC_REJECT_SAMPLE)
    assert result == ("LLM_PROVIDER_REJECTED", 502)


def test_classify_image_url_invalid_parameter():
    text = (
        "litellm.BadRequestError: invalid_parameter_error with image_url "
        "content block rejected"
    )
    assert classify_llm_provider_error(text) == ("LLM_MODALITY_UNSUPPORTED", 502)


def test_normal_chinese_answer_not_classified():
    assert classify_llm_provider_error("根据设备手册，轴承间隙应在 0.05–0.08mm。") is None
    assert classify_llm_provider_error("") is None
    assert classify_llm_provider_error("请检查账单与配额配置说明。") is None
