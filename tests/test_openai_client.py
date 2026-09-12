import json

import httpx

from deepsearch_agent.config import AppConfig
from deepsearch_agent.openai_client import OpenAICompatibleClient


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
        config=AppConfig(),
        client=httpx.Client(transport=transport),
    )

    answer = client.complete("hello")

    assert answer == "模型回答"