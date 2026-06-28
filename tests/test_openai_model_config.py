from unittest.mock import patch, sentinel

import pytest

from agent.utils import model


@pytest.fixture(autouse=True)
def clear_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CODEX_PROXY_BASE_URL",
        "CODEX_PROXY_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "DASHBOARD_BASE_URL",
        "LLM_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_make_model_uses_codex_proxy_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "http://codex-proxy:8080/v1/")
    monkeypatch.setenv("CODEX_PROXY_API_KEY", "proxy-key")
    with patch("agent.utils.model.init_chat_model", return_value=sentinel.chat_model) as init:
        result = model.make_model("openai:gpt-5.5", max_tokens=16_000)

        assert result is sentinel.chat_model
        init.assert_called_once_with(
            model="openai:gpt-5.5",
            max_tokens=16_000,
            max_retries=model.DEFAULT_MAX_RETRIES,
            base_url="http://codex-proxy:8080/v1",
            use_responses_api=False,
            api_key="proxy-key",
        )


def test_make_model_uses_openai_base_url_with_codex_proxy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex-proxy.example.com/v1")
    monkeypatch.setenv("CODEX_PROXY_API_KEY", "proxy-key")
    with patch("agent.utils.model.init_chat_model", return_value=sentinel.chat_model) as init:
        model.make_model("openai:gpt-5.5")

    kwargs = init.call_args.kwargs
    assert kwargs["base_url"] == "https://codex-proxy.example.com/v1"
    assert kwargs["api_key"] == "proxy-key"
    assert kwargs["use_responses_api"] is True


def test_make_model_does_not_send_codex_proxy_key_to_default_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_PROXY_API_KEY", "proxy-key")
    with patch("agent.utils.model.init_chat_model", return_value=sentinel.chat_model) as init:
        model.make_model("openai:gpt-5.5")

    kwargs = init.call_args.kwargs
    assert kwargs["base_url"] == model.OPENAI_RESPONSES_WS_BASE_URL
    assert "api_key" not in kwargs


def test_make_model_uses_openai_api_key_for_default_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    with patch("agent.utils.model.init_chat_model", return_value=sentinel.chat_model) as init:
        model.make_model("openai:gpt-5.5")

    kwargs = init.call_args.kwargs
    assert kwargs["base_url"] == model.OPENAI_RESPONSES_WS_BASE_URL
    assert kwargs["api_key"] == "openai-key"
    assert kwargs["use_responses_api"] is True


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_provider_model_kwargs_sets_reasoning_effort_for_codex_proxy(
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "https://codexproxy.example.com/v1")

    kwargs = model.provider_model_kwargs("openai:gpt-5.5", effort, max_tokens=16_000)

    assert kwargs == {"max_tokens": 16_000, "reasoning_effort": effort}


def test_provider_model_kwargs_omits_unsupported_reasoning_effort_for_codex_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "https://codexproxy.example.com/v1")

    kwargs = model.provider_model_kwargs("openai:gpt-5.5", "none", max_tokens=16_000)

    assert kwargs == {"max_tokens": 16_000}


def test_validate_local_dev_llm_config_accepts_codex_proxy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://localhost:5173")
    monkeypatch.setenv("LLM_MODEL_ID", "openai:gpt-5.5")
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "http://codex-proxy:8080/v1")
    monkeypatch.setenv("CODEX_PROXY_API_KEY", "proxy-key")

    model.validate_local_dev_llm_config()


def test_validate_local_dev_llm_config_rejects_codex_proxy_key_without_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://localhost:5173")
    monkeypatch.setenv("LLM_MODEL_ID", "openai:gpt-5.5")
    monkeypatch.setenv("CODEX_PROXY_API_KEY", "proxy-key")

    with pytest.raises(ValueError, match="CODEX_PROXY"):
        model.validate_local_dev_llm_config()
