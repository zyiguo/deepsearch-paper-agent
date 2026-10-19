from typing import Protocol, runtime_checkable, ContextManager
from pathlib import Path

@runtime_checkable
class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class ImageLLMClient(Protocol):
    def complete_images(self, prompt: str, images: list[Path], *, structured: bool = False) -> str: ...


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ImageEmbeddingClient(Protocol):
    def embed_images(self, images: list[Path]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class EventSink(Protocol):
    def __call__(self, kind: str, payload: dict) -> None: ...


class RunRecorder(Protocol):
    def capture(self, metadata: dict) -> ContextManager[EventSink | None]: ...


class AnswerStore(Protocol):
    def append(self, *, question: str, answer) -> None: ...


class VisualEvidenceProvider(Protocol):
    def complete(self, prompt: str, evidence: list[dict], *, structured: bool = True) -> str: ...
