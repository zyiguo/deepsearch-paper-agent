"""Cloud image embeddings, independent of text embeddings and chat models."""
import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from deepsearch_agent.embeddings import normalize
from deepsearch_agent.llm_gateway import ModelServiceError


@dataclass(frozen=True)
class ImageEmbeddingSettings:
    backend: str = 'disabled'
    api_key: str = field(default='', repr=False)
    base_url: str = ''
    model: str = 'Qwen/Qwen3-VL-Embedding-8B'
    dimensions: int = 1024
    batch_size: int = 1

    def __post_init__(self):
        if self.backend not in {'disabled', 'siliconflow'}:
            raise ValueError('Unsupported image embedding backend')
        if (type(self.dimensions) is not int or self.dimensions <= 0
                or type(self.batch_size) is not int or not 1 <= self.batch_size <= 4):
            raise ValueError('Invalid image embedding dimensions or batch size')
        if self.backend != 'disabled':
            url = urlsplit(self.base_url)
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.path.rstrip('/').endswith('/embeddings')
                    or not self.api_key.strip() or not self.model.strip()):
                raise ValueError('Invalid image embedding credentials or URL')

    @property
    def identity(self):
        data = [self.backend, self.base_url.rstrip('/'), self.model, self.dimensions, 'page-image-v1']
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:24]

    @classmethod
    def load(cls, path: Path, *, shared_embeddings=None, environ=None):
        values = dict(os.environ if environ is None else environ)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path, encoding='utf-8-sig', interpolate=False).items() if v is not None})
        try:
            reuse = values.get('IMAGE_EMBEDDING_USE_EMBEDDING_CREDENTIALS', 'false').strip().lower()
            if reuse not in {'true', 'false'}:
                raise ValueError()
            shared = reuse == 'true'
            if shared and (shared_embeddings is None or shared_embeddings.backend == 'demo'):
                raise ValueError()
            return cls(
                backend=values.get('IMAGE_EMBEDDING_BACKEND', 'disabled').strip().lower(),
                api_key=shared_embeddings.api_key if shared and shared_embeddings else values.get('IMAGE_EMBEDDING_API_KEY', ''),
                base_url=shared_embeddings.base_url if shared and shared_embeddings else values.get('IMAGE_EMBEDDING_BASE_URL', ''),
                model=values.get('IMAGE_EMBEDDING_MODEL', 'Qwen/Qwen3-VL-Embedding-8B').strip(),
                dimensions=int(values.get('IMAGE_EMBEDDING_DIMENSIONS', '1024')),
                batch_size=int(values.get('IMAGE_EMBEDDING_BATCH_SIZE', '1')),
            )
        except (ValueError, TypeError):
            raise ValueError('Image Embedding configuration invalid; check .env.image_embedding') from None


class SiliconFlowImageEmbeddingClient:
    def __init__(self, settings: ImageEmbeddingSettings, transport=None):
        self.settings = settings
        self.transport = transport

    def embed_images(self, images: list[Path]) -> list[list[float]]:
        vectors = []
        # Encode only one bounded batch at a time, not a whole 100-page PDF.
        for start in range(0, len(images), self.settings.batch_size):
            inputs = []
            for image in images[start:start + self.settings.batch_size]:
                if image.stat().st_size > 10 * 1024 * 1024:
                    raise ValueError('Image embeddings require PNG files under 10 MiB')
                data = image.read_bytes()
                if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise ValueError('Image embeddings require PNG files under 10 MiB')
                inputs.append({'image': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')})
            vectors.extend(self._embed(inputs))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Image retrieval query cannot be blank')
        return self._embed([{'text': text}])[0]

    def _embed(self, inputs: list[dict]) -> list[list[float]]:
        vectors = []
        try:
            with httpx.Client(timeout=120, follow_redirects=False, transport=self.transport) as client:
                for start in range(0, len(inputs), self.settings.batch_size):
                    batch = inputs[start:start + self.settings.batch_size]
                    response = client.post(
                        self.settings.base_url.rstrip('/') + '/embeddings',
                        headers={'Authorization': 'Bearer ' + self.settings.api_key},
                        json={'model': self.settings.model, 'input': batch, 'encoding_format': 'float',
                              'dimensions': self.settings.dimensions},
                    )
                    if not response.is_success:
                        raise ModelServiceError(f'Image Embedding request failed (HTTP {response.status_code}); check model access and quota')
                    try:
                        items = response.json()['data']
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
                        raise ModelServiceError('Image Embedding returned invalid vectors; index unchanged') from None
        except httpx.TimeoutException:
            raise ModelServiceError('Image Embedding request timed out', 504) from None
        except httpx.RequestError:
            raise ModelServiceError('Cannot connect to Image Embedding service') from None
        return vectors
