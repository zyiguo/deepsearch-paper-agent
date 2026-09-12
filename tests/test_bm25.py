"""Tests for BM25 tokenization, incremental indexing, and score normalization."""
import pytest

from deepsearch_agent.bm25 import (
    BM25Index,
    normalize_scores,
    tokenize,
    weighted_score_fusion,
)


# ── tokenize ─────────────────────────────────────────────────────────

def test_tokenize_chinese_uses_jieba_segmentation():
    tokens = tokenize('对比学习训练编码器')
    assert '训练' in tokens
    assert '对比' in tokens


def test_tokenize_casefolds_english():
    assert tokenize('BM25 SEARCH') == ['bm25', 'search']


def test_tokenize_keeps_hyphenated_identifier_as_one_token():
    assert tokenize('AX-247') == ['ax-247']


def test_tokenize_drops_stopwords():
    assert tokenize('什么的 the') == []


def test_tokenize_mixed_text_keeps_both_languages():
    tokens = tokenize('RAG 检索增强生成')
    assert 'rag' in tokens
    assert any('检索' in token for token in tokens)


# ── incremental index ────────────────────────────────────────────────

DOCS = [
    '对比学习训练视觉编码器',
    'BM25 ranking with tf-idf style scoring',
    '灯塔每隔十二秒闪一次白光',
    '仪器型号 AX-247 采样频率 100 Hz',
]


def test_incremental_add_matches_full_build():
    full = BM25Index(DOCS)
    incremental = BM25Index(DOCS[:2])
    incremental.add_documents(DOCS[2:])

    assert len(incremental) == len(full) == 4
    for query in ['对比学习', 'ax-247', '灯塔 白光', 'bm25 scoring', '仪器']:
        assert incremental.rank(query, 4) == full.rank(query, 4)
        inc_scores = dict(incremental.rank_with_scores(query, 4))
        full_scores = dict(full.rank_with_scores(query, 4))
        assert inc_scores == pytest.approx(full_scores)


def test_add_documents_on_empty_index():
    index = BM25Index()
    assert len(index) == 0
    index.add_documents(['内容一', '内容二'])
    assert len(index) == 2


def test_average_length_updates_incrementally():
    index = BM25Index(['一 二 三', '一 二'])
    assert index.average_length == pytest.approx(2.5)
    index.add_documents(['一'])
    assert index.average_length == pytest.approx(2.0)


def test_rank_with_scores_returns_sorted_score_pairs():
    index = BM25Index(DOCS)
    scored = index.rank_with_scores('灯塔 白光', 4)

    assert scored
    assert scored[0][0] == 2
    scores = [score for _, score in scored]
    assert scores == sorted(scores, reverse=True)


def test_empty_index_ranks_nothing():
    assert BM25Index().rank_with_scores('任何查询', 5) == []
    assert BM25Index().rank('任何查询', 5) == []


# ── score normalization & fusion ─────────────────────────────────────

def test_normalize_scores_min_max_scales_to_unit_interval():
    normalized = normalize_scores([(5, 8.0), (2, 6.0), (9, 4.0)])
    assert normalized == [(5, 1.0), (2, 0.5), (9, 0.0)]


def test_normalize_scores_empty_returns_empty():
    assert normalize_scores([]) == []


def test_normalize_scores_single_hit_becomes_one():
    assert normalize_scores([(3, 2.5)]) == [(3, 1.0)]


def test_normalize_scores_all_equal_become_one():
    assert normalize_scores([(0, 1.2), (1, 1.2)]) == [(0, 1.0), (1, 1.0)]


def test_weighted_fusion_balanced_weights_prefer_dual_signal():
    vector = [(0, 0.9), (1, 0.3)]   # normalized: 0 → 1.0, 1 → 0.0
    lexical = [(1, 5.0), (0, 0.0)]  # normalized: 1 → 1.0, 0 → 0.0
    # 0: 0.5, 1: 0.5 → tie broken by index
    assert weighted_score_fusion([vector, lexical], [0.5, 0.5], 2) == [0, 1]


def test_weighted_fusion_weights_shift_winner():
    vector = [(0, 0.9), (1, 0.3)]
    lexical = [(1, 5.0), (0, 0.0)]
    # 0: 0.8, 1: 0.2 → vector-dominant
    assert weighted_score_fusion([vector, lexical], [0.8, 0.2], 2) == [0, 1]
    # 0: 0.2, 1: 0.8 → lexical-dominant
    assert weighted_score_fusion([vector, lexical], [0.2, 0.8], 2) == [1, 0]


def test_weighted_fusion_missing_index_contributes_zero():
    vector = [(0, 0.9)]
    lexical = [(1, 3.0)]
    # each list has a single hit → both normalize to 1.0; weights decide
    assert weighted_score_fusion([vector, lexical], [0.7, 0.3], 2) == [0, 1]
    assert weighted_score_fusion([vector, lexical], [0.3, 0.7], 2) == [1, 0]


def test_weighted_fusion_respects_limit_and_rejects_bad_input():
    vector = [(0, 0.9), (1, 0.3)]
    assert weighted_score_fusion([vector], [1.0], 1) == [0]
    assert weighted_score_fusion([], [], 5) == []
    assert weighted_score_fusion([vector], [1.0], 0) == []
