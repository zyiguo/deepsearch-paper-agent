"""Configurable embeddings, isolated from chat-model credentials."""
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from deepsearch_agent.adapters.demo import build_demo_knowledge_base
from deepsearch_agent.knowledge.vector_math import normalize
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.adapters.governed_calls import metered
from deepsearch_agent.core.governance import report_provider_usage


@dataclass(frozen=True)
class EmbeddingSettings:
    backend: str = "demo"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    batch_size: int = 10
    retrieval_mode: str = 'hybrid'

    def __post_init__(self):
        if self.retrieval_mode not in {'semantic', 'bm25', 'hybrid'}:
            raise ValueError('RETRIEVAL_MODE 必须是 semantic、bm25 或 hybrid')
        if self.backend not in {"demo", "openai_compatible"}:
            raise ValueError("EMBEDDING_BACKEND 必须是 demo 或 openai_compatible")
        if self.dimensions <= 0 or not 1 <= self.batch_size <= 10:
            raise ValueError("向量维度必须为正数，批次大小必须在 1 到 10 之间")
        if self.backend == "demo":
            return
        url = urlsplit(self.base_url)
        if not self.api_key.strip():
            raise ValueError("请在 .env.embedding 填写 EMBEDDING_API_KEY")
        if (url.scheme != "https" or not url.hostname or url.username
                or url.password or url.query or url.fragment
                or url.path.rstrip('/').endswith('/embeddings')):
            raise ValueError("EMBEDDING_BASE_URL 必须是 HTTPS 基础地址，不含 /embeddings")
        if not self.model.strip():
            raise ValueError("EMBEDDING_MODEL 不能为空")

    @classmethod
    def load(cls, path: Path):
        values = dict(os.environ)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
        try:
            return cls(
                backend=values.get("EMBEDDING_BACKEND", "demo").strip(),
                api_key=values.get("EMBEDDING_API_KEY", "").strip(),
                base_url=values.get("EMBEDDING_BASE_URL", "").strip().rstrip('/'),
                model=values.get("EMBEDDING_MODEL", "text-embedding-v4").strip(),
                dimensions=int(values.get("EMBEDDING_DIMENSIONS", "1024")),
                batch_size=int(values.get("EMBEDDING_BATCH_SIZE", "10")),
                retrieval_mode=values.get('RETRIEVAL_MODE', 'hybrid').strip().lower(),
            )
        except ValueError as error:
            # Never echo configuration values (which may contain credentials).
            raise ValueError("Embedding 配置无效，请检查 .env.embedding 的密钥、地址、模型和数字参数") from None

    @property
    def identity(self):
        # Credentials and batch size do not affect the vector space.
        data = [self.base_url.rstrip('/'), self.model, self.dimensions, "dense-v1"]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:24]


class RemoteEmbeddingClient:
    def __init__(self, settings: EmbeddingSettings, transport=None):
        self.settings = settings
        self.transport = transport

    @metered('embedding')
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding 输入不能为空")
        vectors = []
        try:
            with httpx.Client(timeout=60, follow_redirects=False, transport=self.transport) as client:
                for start in range(0, len(texts), self.settings.batch_size):
                    batch = texts[start:start + self.settings.batch_size]
                    payload = {"model": self.settings.model, "input": batch,
                               "encoding_format": "float"}
                    # SiliconFlow BGE-M3 has a fixed output size and rejects dimensions.
                    # Still validate every returned vector against the configured size.
                    fixed_bge = (urlsplit(self.settings.base_url).hostname == "api.siliconflow.cn"
                                 and self.settings.model in {"BAAI/bge-m3", "Pro/BAAI/bge-m3"})
                    if not fixed_bge:
                        payload["dimensions"] = self.settings.dimensions
                    response = client.post(
                        f"{self.settings.base_url.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {self.settings.api_key}"},
                        json=payload,
                    )
                    if not response.is_success:
                        reasons = {401: "认证失败", 403: "没有模型权限", 429: "限流或额度不足"}
                        reason = reasons.get(response.status_code, "请求失败，请检查服务、模型与参数")
                        raise ModelServiceError(f"Embedding {reason}（HTTP {response.status_code}）")
                    try:
                        body = response.json()
                        report_provider_usage(body.get('usage'))
                        items = body["data"]
                        if not isinstance(items, list) or len(items) != len(batch):
                            raise ValueError()
                        if any(type(item['index']) is not int for item in items):
                            raise ValueError()
                        items = sorted(items, key=lambda item: item['index'])
                        if [item['index'] for item in items] != list(range(len(batch))):
                            raise ValueError()
                        for item in items:
                            vector = item['embedding']
                            if (not isinstance(vector, list) or len(vector) != self.settings.dimensions
                                    or any(type(x) not in (int, float) for x in vector)):
                                raise ValueError()
                            vectors.append(normalize(vector))
                    except (ValueError, KeyError, TypeError, OverflowError):
                        raise ModelServiceError("Embedding 返回格式、索引或向量维度异常，本次结果未入库") from None
        except httpx.TimeoutException:
            raise ModelServiceError("Embedding 请求超时，请核对知识库状态后重试", 504) from None
        except httpx.RequestError:
            raise ModelServiceError("无法连接 Embedding 服务，请检查接口地址和网络") from None
        return vectors


def build_knowledge_base(settings: EmbeddingSettings):
    kb = build_demo_knowledge_base()
    if settings.backend != "demo":
        kb.embedding_client = RemoteEmbeddingClient(settings)
        kb.embedding_id = f"remote-{settings.identity}"
        kb.retrieval_mode = settings.retrieval_mode
    return kb
