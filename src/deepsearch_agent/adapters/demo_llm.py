from deepsearch_agent.core.models import LLMResponse

class FakeLLMClient:
    def __init__(self, response: str):
        self.response = response

    def generate(self, prompt: str) -> LLMResponse:
        return LLMResponse(text=self.response, model="demo")

    def complete(self, prompt: str) -> str:
        return self.generate(prompt).text

    def close(self) -> None:
        pass
