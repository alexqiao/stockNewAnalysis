from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from trade_news_analysis.config import Settings
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.x_posts import XPostScreener


@pytest.fixture
def llm_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_THINKING"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=\n", encoding="utf-8")
    return Settings(_env_file=env_file)


def test_openrouter_defaults_leave_model_calls_disabled(llm_settings: Settings) -> None:
    assert llm_settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert llm_settings.llm_model == "stealth/space-bunny-alpha"
    assert llm_settings.llm_thinking is None
    assert not llm_settings.llm_configured


@pytest.mark.parametrize("service", ["analysis", "x_posts"])
def test_services_call_openrouter_with_configured_key(
    service: str, llm_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-openrouter-key")
    configured = Settings(_env_file=None)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": configured.llm_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        def client_factory(**kwargs: Any) -> OpenAI:
            return OpenAI(**kwargs, http_client=http_client)

        with patch(
            f"trade_news_analysis.services.{service}.OpenAI", side_effect=client_factory
        ):
            if service == "analysis":
                result = EventAnalyzer(configured)._complete("system", "prompt")
            else:
                result = XPostScreener(configured)._complete("prompt")

    assert result == "{}"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-openrouter-key"
    payload = json.loads(request.content)
    assert payload["model"] == "stealth/space-bunny-alpha"
    assert payload["messages"][-1] == {"role": "user", "content": "prompt"}
    assert "thinking" not in payload


@pytest.mark.parametrize("service", ["analysis", "x_posts"])
def test_services_do_not_create_client_without_key(service: str, llm_settings: Settings) -> None:
    with patch(f"trade_news_analysis.services.{service}.OpenAI") as client_class:
        with pytest.raises(RuntimeError, match="LLM未配置"):
            if service == "analysis":
                EventAnalyzer(llm_settings)._complete("system", "prompt")
            else:
                XPostScreener(llm_settings)._complete("prompt")
        client_class.assert_not_called()
