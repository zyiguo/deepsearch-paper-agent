from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from pathlib import Path


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str | None = None
    usage: TokenUsage | None = None
    request_id: str | None = None


class ModelServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class ImageLLMClient(Protocol):
    def complete_images(self, prompt: str, images: list[Path], *, structured: bool = False) -> str: ...


class FakeLLMClient:
    def __init__(self, response: str):
        self.response = response

    def generate(self, prompt: str) -> LLMResponse:
        return LLMResponse(text=self.response, model="demo")

    def complete(self, prompt: str) -> str:
        return self.generate(prompt).text

    def close(self) -> None:
        pass
