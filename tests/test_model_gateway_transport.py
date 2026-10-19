import json

import httpx

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "模型回答"}}]},
    )


def test_openai_compatible_client_calls_chat_completions_and_parses_content(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    transport = httpx.MockTransport(_handler)
    client = OpenAICompatibleClient(
        settings=LLMSettings(backend="openai_compatible", api_key="test-key",
                             base_url="https://api.openai.com/v1", model="gpt-4o-mini"),
        transport=transport,
    )

    try:
        answer = client.complete("hello")
    finally:
        client.close()

    assert answer == "模型回答"