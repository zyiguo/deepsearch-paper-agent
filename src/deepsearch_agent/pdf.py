"""PDF reading interface and a fake implementation for testing."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class PdfReader(Protocol):
    def read(self, path: str) -> list[str]: ...


class FakePdfReader:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages

    def read(self, path: str) -> list[str]:
        return self.pages