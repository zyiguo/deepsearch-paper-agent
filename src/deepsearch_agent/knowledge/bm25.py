"""In-memory BM25 and score fusion over page-aware text chunks."""
import logging
import math
import re
from collections import Counter, defaultdict

import jieba

jieba.setLogLevel(logging.ERROR)
STOPWORDS = frozenset('的 了 是 在 和 与 及 或 请 什么 哪个 哪些 如何 怎么 为什么 吗 呢 一个 一种 这个 那个 the a an is are of to in and or what how'.split())
PARTS = re.compile(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+', re.I)


def tokenize(text: str) -> list[str]:
    tokens = []
    for part in PARTS.findall(text.casefold()):
        if re.fullmatch(r'[\u3400-\u9fff]+', part):
            words = jieba.cut_for_search(part, HMM=False)
        else:
            words = [part]
        tokens.extend(word for word in words if word and word not in STOPWORDS)
    return tokens


class BM25Index:
    """Okapi BM25 index that supports incremental document addition.

    ``add_documents`` only tokenizes the new texts and extends the postings,
    so ingesting another PDF into a large knowledge base no longer
    re-tokenizes and re-scores the whole corpus.
    """

    def __init__(self, documents: list[str] | None = None, k1: float = 1.5, b: float = .75):
        self.k1, self.b = k1, b
        self.frequencies: list[Counter] = []
        self.lengths: list[int] = []
        self.total_length = 0
        self.postings: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
        if documents:
            self.add_documents(documents)

    def __len__(self) -> int:
        return len(self.frequencies)

    @property
    def average_length(self) -> float:
        return self.total_length / len(self.frequencies) if self.frequencies else 0.0

    def add_documents(self, documents: list[str]) -> None:
        for text in documents:
            frequency = Counter(tokenize(text))
            index = len(self.frequencies)
            self.frequencies.append(frequency)
            self.lengths.append(sum(frequency.values()))
            self.total_length += self.lengths[-1]
            for term, count in frequency.items():
                self.postings[term].append((index, count))

    def rank_with_scores(self, query: str, limit: int) -> list[tuple[int, float]]:
        """Return (document_index, bm25_score) sorted by score desc, index asc."""
        if limit <= 0 or not self.average_length:
            return []
        scores: defaultdict[int, float] = defaultdict(float)
        count = len(self.frequencies)
        average = self.average_length
        for term in sorted(set(tokenize(query))):
            postings = self.postings.get(term, [])
            df = len(postings)
            idf = math.log1p((count - df + .5) / (df + .5))
            for index, tf in postings:
                normalizer = self.k1 * (1 - self.b + self.b * self.lengths[index] / average)
                scores[index] += idf * tf * (self.k1 + 1) / (tf + normalizer)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]

    def rank(self, query: str, limit: int) -> list[int]:
        return [index for index, _ in self.rank_with_scores(query, limit)]


def normalize_scores(scored: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Min-max normalize one retriever's scores to [0, 1].

    A set whose scores are all equal (single hit or exact ties) normalizes
    to 1.0 so that presence still outweighs absence during fusion.
    """
    if not scored:
        return []
    values = [score for _, score in scored]
    low, high = min(values), max(values)
    if high - low < 1e-12:
        return [(index, 1.0) for index, _ in scored]
    span = high - low
    return [(index, (score - low) / span) for index, score in scored]


def weighted_score_fusion(
    scored_lists: list[list[tuple[int, float]]],
    weights: list[float],
    limit: int,
) -> list[int]:
    """Fuse per-retriever score lists by weighted sum after normalization.

    Each list is min-max normalized first so heterogeneous scales (unbounded
    BM25 scores vs bounded cosine similarities) contribute comparably; an
    index absent from a list contributes 0 for that retriever.
    """
    if limit <= 0 or not scored_lists or len(weights) != len(scored_lists):
        return []
    fused: defaultdict[int, float] = defaultdict(float)
    for scored, weight in zip(scored_lists, weights, strict=True):
        for index, score in normalize_scores(scored):
            fused[index] += weight * score
    return sorted(fused, key=lambda index: (-fused[index], index))[:limit]


def reciprocal_rank_fusion(rankings: list[list[int]], limit: int, constant: int = 60) -> list[int]:
    scores = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for rank, index in enumerate(ranking, start=1):
            if index not in seen:
                scores[index] += 1 / (constant + rank)
                seen.add(index)
    return sorted(scores, key=lambda i: (-scores[i], i))[:limit]
