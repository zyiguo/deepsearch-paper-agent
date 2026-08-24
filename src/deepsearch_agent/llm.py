"""LLM client interface and a fake implementation for testing."""


class FakeLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response

    def complete(self, prompt: str) -> str:
        return self.response