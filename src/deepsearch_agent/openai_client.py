"""OpenAI-compatible HTTP client implementing the LLMClient protocol."""

import os

import httpx

from deepsearch_agent.config import AppConfig


class OpenAICompatibleClient:
    def __init__(
        self,
        config: AppConfig,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.Client()

    def complete(self, prompt: str) -> str:
        api_key = os.environ[self.config.llm_api_key_env]
        response = self._client.post(
            f"{self.config.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": self.config.llm_model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]