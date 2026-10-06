from math import isfinite, sqrt
from typing import Protocol
from pathlib import Path

import httpx


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ImageEmbeddingClient(Protocol):
    def embed_images(self, images: list[Path]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def normalize(vector: list[float]) -> list[float]:
    if not vector or not all(isfinite(value) for value in vector):
        raise ValueError("向量必须非空，且只能包含有限数值")

    length = sqrt(sum(value * value for value in vector))

    if length == 0 or not isfinite(length):
        raise ValueError("向量长度必须是有限的正数")

    return [value / length for value in vector]


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        batch_size: int = 16,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if any(not text.strip() for text in texts):
            raise ValueError("不能为纯空白文本生成向量")

        vectors: list[list[float]] = []

        with httpx.Client(timeout=60.0) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start:start + self.batch_size]

                response = client.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json={
                        "model": self.model,
                        "input": batch,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()

                items = sorted(
                    response.json()["data"],
                    key=lambda item: item["index"],
                )

                if [item["index"] for item in items] != list(range(len(batch))):
                    raise ValueError("Embedding 返回的索引与输入不匹配")

                vectors.extend(
                    [float(value) for value in item["embedding"]]
                    for item in items
                )

        return vectors
