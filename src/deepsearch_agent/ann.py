"""Approximate nearest-neighbour search over normalized embedding vectors.

Below ``exact_threshold`` vectors an exact numpy scan answers queries —
approximation only pays off once the corpus is large enough for the
recall/speed trade-off to matter, and small corpora keep 100% recall.
Above the threshold an IVF (inverted file) index clusters vectors with
k-means and only searches the ``n_probe`` clusters nearest to the query.

All vectors are assumed pre-normalized, so cosine similarity is a plain
dot product.  ``add`` after the IVF structure exists assigns new vectors
to their nearest existing centroid instead of retraining k-means, keeping
ingestion incremental.
"""
import numpy as np


class AnnIndex:
    def __init__(
        self,
        dimension: int,
        exact_threshold: int = 1024,
        n_probe: int | None = None,
        seed: int = 42,
    ) -> None:
        if dimension <= 0:
            raise ValueError("向量维度必须为正数")
        self.dimension = dimension
        self.exact_threshold = max(1, exact_threshold)
        self.n_probe = n_probe
        self.seed = seed
        self._matrix = np.zeros((0, dimension), dtype=np.float64)
        self._centroids: np.ndarray | None = None
        self._lists: list[list[int]] = []

    def __len__(self) -> int:
        return len(self._matrix)

    @property
    def is_approximate(self) -> bool:
        return self._centroids is not None

    def add(self, vectors: list[list[float]]) -> None:
        """Append vectors, building the IVF structure once the threshold is hit."""
        if not vectors:
            return
        incoming = np.asarray(vectors, dtype=np.float64)
        if incoming.ndim != 2 or incoming.shape[1] != self.dimension:
            raise ValueError("向量维度不一致")
        start = len(self._matrix)
        self._matrix = incoming.copy() if not start else np.vstack([self._matrix, incoming])
        if self._centroids is None:
            if len(self._matrix) >= self.exact_threshold:
                self._build_ivf()
        else:
            assignments = np.argmax(self._centroids @ incoming.T, axis=0)
            for row, centroid in enumerate(assignments):
                self._lists[int(centroid)].append(start + row)

    def _build_ivf(self) -> None:
        count = len(self._matrix)
        n_lists = min(max(4, int(count ** 0.5)), 64, count)
        rng = np.random.default_rng(self.seed)
        centroids = self._matrix[rng.choice(count, size=n_lists, replace=False)].copy()
        for _ in range(5):
            assignments = np.argmax(centroids @ self._matrix.T, axis=0)
            for c in range(n_lists):
                members = self._matrix[assignments == c]
                if len(members):
                    centroids[c] = members.mean(axis=0)
        self._centroids = centroids
        self._lists = [[] for _ in range(n_lists)]
        for row, centroid in enumerate(np.argmax(centroids @ self._matrix.T, axis=0)):
            self._lists[int(centroid)].append(row)

    def _probe_count(self) -> int:
        lists = len(self._lists)
        if self.n_probe is not None:
            return max(1, min(self.n_probe, lists))
        if lists <= 8:
            return lists  # tiny corpora: probing every list keeps recall exact
        return max(1, lists // 8)

    def search(self, query: list[float], top_k: int) -> list[tuple[int, float]]:
        """Return (row_index, cosine) pairs sorted by score desc, index asc."""
        if top_k <= 0 or not len(self._matrix):
            return []
        vector = np.asarray(query, dtype=np.float64)
        if vector.shape != (self.dimension,):
            raise ValueError("查询向量维度不一致")

        if self._centroids is None:
            scores = self._matrix @ vector
            order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:top_k]
            return [(i, float(scores[i])) for i in order]

        centroid_scores = self._centroids @ vector
        probes = np.argsort(-centroid_scores)[:self._probe_count()]
        candidates = sorted(
            row for centroid in probes for row in self._lists[int(centroid)]
        )
        if not candidates:
            # Degenerate clustering: probe lists empty — fall back to exact
            # scan rather than returning nothing.
            scores = self._matrix @ vector
            order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:top_k]
            return [(i, float(scores[i])) for i in order]

        rows = np.asarray(candidates)
        scores = self._matrix[rows] @ vector
        order = sorted(range(len(rows)), key=lambda r: (-scores[r], int(rows[r])))[:top_k]
        return [(int(rows[r]), float(scores[r])) for r in order]
