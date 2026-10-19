"""Tests for the token estimation utility."""
from deepsearch_agent.core.tokens import estimate_tokens, truncate_text


def test_estimate_tokens_empty():
    assert estimate_tokens('') == 0


def test_estimate_tokens_chinese():
    text = '实验使用对比学习训练编码器'
    # Pure CJK: ~1 token per char, rounded up.
    assert estimate_tokens(text) == len(text)


def test_estimate_tokens_english():
    text = 'contrastive learning encoder model'
    # Pure ASCII: ceil(len * 0.25)
    assert estimate_tokens(text) == -(-len(text) // 4)


def test_estimate_tokens_mixed():
    text = '模型model训练training'
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    assert estimate_tokens(text) == -(-(cjk + other * 0.25) // 1)  # ceil


def test_truncate_text_short_enough_returns_unchanged():
    text = '这是一段短文本。'
    assert truncate_text(text, 100) == text


def test_truncate_text_cuts_at_sentence_boundary():
    text = '第一句话。第二句话。第三句话。第四句话。'
    result = truncate_text(text, 10)
    assert result.endswith('。…')
    assert len(result) < len(text)


def test_truncate_text_empty():
    assert truncate_text('', 100) == ''
    assert truncate_text('abc', 0) == ''
