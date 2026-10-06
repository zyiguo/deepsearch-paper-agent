"""Page-image vector space and rank fusion; no PDF or cloud dependencies."""
from copy import copy
from dataclasses import asdict
from pathlib import Path

from deepsearch_agent.ann import AnnIndex
from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.embeddings import ImageEmbeddingClient, normalize
from deepsearch_agent.content_identity import image_content_id


class ImageVectorIndex:
    def __init__(self, identity: str, client: ImageEmbeddingClient | None = None):
        self.identity = identity
        self.client = client
        self.chunks: list[TextChunk] = []
        self.vectors: list[list[float]] = []
        self._ann = None

    def prepare(self, source: str, images: list[Path]):
        if self.client is None:
            raise ValueError('Image embedding client is disabled')
        vectors = self.client.embed_images(images)
        if len(vectors) != len(images):
            raise ValueError('Image vector count mismatch')
        entries = [
            {'chunk': asdict(TextChunk(text=f'Original page image {page}; read the attached image for evidence.',
                                      source_path=source, page_number=page, chunk_index=page - 1,
                                      evidence_kind='image', visual_pages=[page],
                                      content_id=image_content_id(path.read_bytes()))), 'vector': vector}
            for page, (path, vector) in enumerate(zip(images, vectors, strict=True), 1)
        ]
        return self.restore({'identity': self.identity, 'entries': entries}, self.client)

    def restrict_sources(self, sources: set[str]):
        result = copy(self)
        indices = [i for i, chunk in enumerate(self.chunks) if chunk.source_path in sources]
        result.chunks = [self.chunks[i] for i in indices]
        result.vectors = [self.vectors[i] for i in indices]
        result._ann = None
        return result

    def merge(self, other):
        if self.identity != other.identity:
            raise ValueError('Image embedding model identity mismatch')
        if self.vectors and other.vectors and len(self.vectors[0]) != len(other.vectors[0]):
            raise ValueError('Image vector dimension mismatch')
        self.chunks.extend(other.chunks)
        self.vectors.extend(other.vectors)
        self._ann = None

    def search(self, query: str, limit: int):
        if not self.chunks or not query.strip() or limit <= 0 or self.client is None:
            return []
        vector = normalize(self.client.embed_query(query))
        if len(vector) != len(self.vectors[0]):
            raise ValueError('Image query vector dimension mismatch')
        if self._ann is None:
            self._ann = AnnIndex(len(vector))
            self._ann.add(self.vectors)
        return [self.chunks[i] for i, _ in self._ann.search(vector, limit)]

    def snapshot(self):
        return {'identity': self.identity, 'entries': [
            {'chunk': asdict(chunk), 'vector': vector}
            for chunk, vector in zip(self.chunks, self.vectors, strict=True)]}

    def hydrate_legacy_identities(self, resolve_image):
        """Recover actual image fingerprints locally without re-embedding."""
        changed = 0
        for chunk in self.chunks:
            if not chunk.content_id:
                try:
                    data = resolve_image(chunk.source_path, chunk.page_number).read_bytes()
                except (KeyError, ValueError, OSError):
                    continue
                chunk.content_id = image_content_id(data)
                chunk.refresh_identity()
                changed += 1
        return changed

    @classmethod
    def restore(cls, payload, client=None, expected_identity=None):
        identity = payload['identity']
        if not isinstance(identity, str) or not identity or (expected_identity and identity != expected_identity):
            raise ValueError('Image embedding model identity mismatch')
        result = cls(identity, client)
        dimension = None
        seen = set()
        for entry in payload['entries']:
            chunk = TextChunk(**entry['chunk'])
            chunk.validate_structure()
            if (chunk.evidence_kind != 'image' or type(chunk.page_number) is not int or chunk.page_number < 1
                    or type(chunk.chunk_index) is not int or chunk.chunk_index < 0
                    or not isinstance(chunk.source_path, str) or not isinstance(chunk.text, str) or not chunk.text.strip()
                    or chunk.visual_pages != [chunk.page_number]):
                raise ValueError('Invalid image index page')
            vector = entry['vector']
            if not isinstance(vector, list) or any(type(x) not in (int, float) for x in vector):
                raise ValueError('Invalid image index vector')
            vector = normalize(vector)
            dimension = dimension if dimension is not None else len(vector)
            configured_dimension = getattr(getattr(client, 'settings', None), 'dimensions', dimension)
            key = (chunk.source_path, chunk.page_number)
            if len(vector) != dimension or len(vector) != configured_dimension or key in seen:
                raise ValueError('Invalid or duplicate image index entry')
            seen.add(key)
            result.chunks.append(chunk)
            result.vectors.append(vector)
        return result


def fuse_rankings(rankings: list[list[TextChunk]], limit: int):
    """RRF compares ranks, never cosine scores from unrelated vector spaces."""
    scores, chunks = {}, {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, 1):
            key = chunk.chunk_id
            chunks.setdefault(key, chunk)
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return [chunks[key] for key in sorted(scores, key=scores.get, reverse=True)[:limit]]
