"""Tests for the LLM-based listwise reranker."""
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.knowledge.reranker import LLMReranker


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.last_prompt = None

    def complete(self, prompt):
        self.last_prompt = prompt
        return self.response


def make_chunks(texts):
    return [TextChunk(text=t, source_path='a.pdf', page_number=i + 1, chunk_index=i)
            for i, t in enumerate(texts)]


def test_rerank_reorders_by_llm_ranking():
    chunks = make_chunks(['第一段', '第二段', '第三段'])
    client = FakeClient('{"ranked_ids": [3, 1, 2]}')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=3)

    assert [c.text for c in result] == ['第三段', '第一段', '第二段']


def test_rerank_respects_top_k():
    chunks = make_chunks(['一', '二', '三', '四'])
    client = FakeClient('{"ranked_ids": [4, 3, 2, 1]}')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=2)

    assert len(result) == 2
    assert [c.text for c in result] == ['四', '三']


def test_rerank_single_chunk_passthrough():
    chunks = make_chunks(['唯一片段'])
    client = FakeClient('{"ranked_ids": [1]}')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=5)

    assert len(result) == 1
    assert result[0].text == '唯一片段'
    assert client.last_prompt is None


def test_rerank_fallback_on_invalid_json():
    chunks = make_chunks(['一', '二', '三'])
    client = FakeClient('not json at all')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=3)

    # Falls back to original order.
    assert [c.text for c in result] == ['一', '二', '三']


def test_rerank_fallback_on_missing_ids():
    chunks = make_chunks(['一', '二', '三'])
    client = FakeClient('{"ranked_ids": [1, 2]}')  # missing 3
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=3)

    assert [c.text for c in result] == ['一', '二', '三']


def test_rerank_fallback_on_duplicate_ids():
    chunks = make_chunks(['一', '二', '三'])
    client = FakeClient('{"ranked_ids": [1, 2, 2]}')  # duplicate, missing 3
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=3)

    assert [c.text for c in result] == ['一', '二', '三']


def test_rerank_fallback_on_runtime_error():
    chunks = make_chunks(['一', '二', '三'])
    class BrokenClient:
        def complete(self, prompt):
            raise RuntimeError('LLM timeout')
    reranker = LLMReranker(BrokenClient())

    result = reranker.rerank('query', chunks, top_k=3)

    assert [c.text for c in result] == ['一', '二', '三']


def test_rerank_strips_code_fences():
    chunks = make_chunks(['一', '二'])
    client = FakeClient('```json\n{"ranked_ids": [2, 1]}\n```')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=2)

    assert [c.text for c in result] == ['二', '一']


def test_rerank_truncates_long_chunks():
    big_text = '这是一段很长的文本。' * 200
    chunks = make_chunks([big_text, big_text, big_text])
    client = FakeClient('{"ranked_ids": [2, 1, 3]}')
    reranker = LLMReranker(client, context_window=512, max_output_tokens=128)

    result = reranker.rerank('query', chunks, top_k=3)

    # Still reranks despite truncation.
    assert [c.text for c in result] == [big_text, big_text, big_text]
    # Prompt was truncated — shorter than full text.
    assert len(client.last_prompt) < len(big_text) * 3


def test_rerank_caps_candidates_at_20():
    chunks = make_chunks([f'片段{i}' for i in range(25)])
    client = FakeClient('{"ranked_ids": ' + str(list(range(20, 0, -1))) + '}')
    reranker = LLMReranker(client)

    result = reranker.rerank('query', chunks, top_k=5)

    assert len(result) == 5
    # Only 20 candidates were sent (ids 1..20), so top-5 is reversed from those.
    assert result[0].text == '片段19'
