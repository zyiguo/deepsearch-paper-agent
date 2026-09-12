"""Tests for the IVF approximate nearest-neighbour index."""
import math

import pytest

from deepsearch_agent.ann import AnnIndex


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def brute_force(vectors: list[list[float]], query: list[float], top_k: int) -> list[tuple[int, float]]:
    scored = [
        (i, sum(a * b for a, b in zip(query, vector)))
        for i, vector in enumerate(vectors)
    ]
    return sorted(scored, key=lambda pair: (-pair[1], pair[0]))[:top_k]


CLUSTERS = [
    [unit([1.0, 0.0, 0.05]), unit([0.98, 0.0, 0.1]), unit([1.0, 0.02, 0.0]), unit([0.95, 0.05, 0.05])],
    [unit([0.0, 1.0, 0.05]), unit([0.0, 0.98, 0.1]), unit([0.02, 1.0, 0.0]), unit([0.05, 0.95, 0.05])],
    [unit([0.05, 0.0, 1.0]), unit([0.1, 0.0, 0.98]), unit([0.0, 0.02, 1.0]), unit([0.05, 0.05, 0.95])],
]
ALL_VECTORS = [vector for cluster in CLUSTERS for vector in cluster]


def test_exact_scan_below_threshold_matches_brute_force():
    index = AnnIndex(3, exact_threshold=1024)
    index.add(ALL_VECTORS)

    assert not index.is_approximate
    for query in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.1, 0.1, 0.9]):
        got = index.search(query, 5)
        want = brute_force(ALL_VECTORS, query, 5)
        assert [i for i, _ in got] == [i for i, _ in want]
        assert [s for _, s in got] == pytest.approx([s for _, s in want])


def test_ivf_finds_cluster_match_with_default_probe():
    index = AnnIndex(3, exact_threshold=4)
    index.add(ALL_VECTORS)

    assert index.is_approximate
    for axis in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]):
        top_index, top_score = index.search(axis, 1)[0]
        assert top_score > 0.95  # a member of the queried cluster ranks first
        expected = CLUSTERS[0] if axis[0] else (CLUSTERS[1] if axis[1] else CLUSTERS[2])
        assert ALL_VECTORS[top_index] in expected


def test_ivf_probing_all_lists_equals_exact_recall():
    index = AnnIndex(3, exact_threshold=4, n_probe=64)
    index.add(ALL_VECTORS)

    for query in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.6, 0.6, 0.2]):
        got = index.search(query, 6)
        want = brute_force(ALL_VECTORS, query, 6)
        assert [i for i, _ in got] == [i for i, _ in want]
        assert [s for _, s in got] == pytest.approx([s for _, s in want])


def test_incremental_add_after_ivf_build():
    index = AnnIndex(3, exact_threshold=4, n_probe=64)
    index.add([vector for cluster in CLUSTERS[:2] for vector in cluster])
    assert index.is_approximate

    index.add(CLUSTERS[2])  # assigned to nearest existing centroids, no retrain
    assert len(index) == 12

    top_index, top_score = index.search([0.0, 0.0, 1.0], 1)[0]
    assert top_score > 0.95
    assert ALL_VECTORS[top_index] in CLUSTERS[2]


def test_build_is_deterministic_for_fixed_seed():
    first = AnnIndex(3, exact_threshold=4)
    first.add(ALL_VECTORS)
    second = AnnIndex(3, exact_threshold=4)
    second.add(ALL_VECTORS)

    assert first.search([1.0, 0.0, 0.0], 5) == second.search([1.0, 0.0, 0.0], 5)


def test_empty_index_and_non_positive_top_k():
    index = AnnIndex(3)
    assert index.search([1.0, 0.0, 0.0], 5) == []
    index.add(ALL_VECTORS)
    assert index.search([1.0, 0.0, 0.0], 0) == []


def test_dimension_mismatch_raises():
    index = AnnIndex(3)
    with pytest.raises(ValueError, match="向量维度不一致"):
        index.add([[1.0, 0.0]])

    index.add(ALL_VECTORS)
    with pytest.raises(ValueError, match="查询向量维度不一致"):
        index.search([1.0, 0.0], 3)


def test_tie_break_prefers_lower_index():
    index = AnnIndex(3, exact_threshold=1024)
    index.add([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    scored = index.search([1.0, 0.0, 0.0], 3)
    assert [i for i, _ in scored] == [0, 1, 2]
