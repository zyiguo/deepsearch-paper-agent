import json
import pytest
from deepsearch_agent.qa import AnswerService
from deepsearch_agent.chunking import TextChunk


class Client:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
    def complete(self, prompt):
        if prompt.startswith('你是一个检索结果重排序器。'):
            import re
            nums = re.findall(r'\[(\d+)\]', prompt)
            return json.dumps({'ranked_ids': list(range(1, len(nums) + 1))})
        if prompt.startswith('你是 Supervisor。'):
            state = json.loads(prompt.split('\ndata: ', 1)[1])
            if state['research_rounds'] == 0:
                action = 'research'
            elif not state['evidence']:
                action = 'stop'
            elif not state['verified']:
                action = 'draft'
            elif state['queries'] and state['research_rounds'] < 3:
                action = 'research'
            else:
                action = 'finish'
            return json.dumps({'action':action, 'tasks':[], 'instruction':'处理当前问题'})
        return json.dumps(next(self.outputs), ensure_ascii=False)


class Library:
    def search(self, query, top_k=5):
        return [TextChunk(text='灯塔每隔十二秒闪白光。', source_path='a.pdf', page_number=1, chunk_index=0)]


def run(outputs, library=None):
    return AnswerService(Client(outputs), library or Library(), use_graph=True).answer('灯塔信号？')


def test_graph_returns_only_verified_claims():
    result = run([{'queries':['灯塔信号']}, {'claims':[{'text':'每隔十二秒闪白光。','citations':[1]}]}, {'supported':[0], 'queries':[]}])
    assert result.text == '每隔十二秒闪白光。 [1]'
    assert len(result.sources) == 1


def test_graph_rejects_fabricated_citation_even_if_verifier_accepts():
    result = run([{'queries':['信号']}] + [{'claims':[{'text':'编造','citations':[99]}]}] * 3)
    assert '编造' not in result.text
    assert result.sources == []


def test_graph_researches_again_and_uses_new_verified_draft():
    result = run([{'queries':['信号']}, {'claims':[{'text':'红光','citations':[1]}]}, {'supported':[], 'queries':['白光 间隔']}, {'queries':['白光 间隔']}, {'claims':[{'text':'白光','citations':[1]}]}, {'supported':[0], 'queries':[]}])
    assert result.text == '白光 [1]'


def test_graph_stops_after_two_supplementary_rounds():
    outputs = [{'queries':['信号']}, {'claims':[{'text':'猜测','citations':[1]}]}, {'supported':[], 'queries':['补查']}]*3
    result = run(outputs)
    assert '无法' in result.text and not result.sources


def test_graph_malformed_output_fails_closed():
    result = run([{'queries':['信号']}, {'wrong':'schema'}, {'wrong':'schema'}])
    assert '无法' in result.text and not result.sources


def test_graph_compacts_sources_to_verified_citations():
    class TwoPages:
        def search(self, query, top_k=5):
            return [TextChunk(text='无关资料', source_path='a.pdf', page_number=1, chunk_index=0),
                    TextChunk(text='白光', source_path='b.pdf', page_number=2, chunk_index=0)]
    result = run([{'queries':['灯塔']}, {'claims':[{'text':'白光','citations':[2]}]}, {'supported':[0], 'queries':[]}], TwoPages())
    assert result.text == '白光 [1]'
    assert len(result.sources) == 1 and result.sources[0].source_path == 'b.pdf'


def test_graph_no_evidence_does_not_generate():
    class Empty:
        def search(self, query, top_k=5):
            return []
    result = run([{'queries':['灯塔']}], Empty())
    assert '无法' in result.text and result.sources == []


def test_graph_model_failure_propagates():
    from deepsearch_agent.llm_gateway import ModelServiceError
    class Broken:
        def complete(self, prompt):
            raise ModelServiceError('模型超时', 504)
    with pytest.raises(ModelServiceError):
        AnswerService(Broken(), Library(), use_graph=True).answer('问题')


def test_graph_disables_inherited_hosted_tracing(monkeypatch):
    from langsmith import utils
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    class PrivateClient(Client):
        def complete(self, prompt):
            assert utils.tracing_is_enabled() is False
            return super().complete(prompt)
    result = AnswerService(PrivateClient([{'queries':['信号']}, {'claims':[]}, {'supported':[], 'queries':[]}]), Library(), use_graph=True).answer('问题')
    assert not result.sources


def test_live_api_and_session_use_graph_and_save_only_final_answer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.llm_config import LLMSettings
    outputs = [{'queries':['模型怎么训练？']}, {'claims':[{'text':'对比学习','citations':[1]}]}, {'supported':[0], 'queries':[]}]*2
    monkeypatch.setattr('deepsearch_agent.api.build_llm', lambda _: Client(outputs))
    settings = LLMSettings(backend='openai_compatible', api_key='test-only', base_url='https://example.com/v1', model='test')
    with TestClient(create_app(index_path=tmp_path/'index.json', llm_settings=settings)) as client:
        client.post('/api/demo/ingest').raise_for_status()
        result = client.post('/api/ask', json={'question':'模型怎么训练？','top_k':1})
        assert result.status_code == 200
        assert result.json()['text'] == '对比学习 [1]'
        sid = client.post('/api/sessions', json={'title':'图流程'}).json()['id']
        result = client.post(f'/api/sessions/{sid}/turns', json={'question':'模型怎么训练？','top_k':1})
        assert result.status_code == 201
        saved = client.get(f'/api/sessions/{sid}').json()['turns']
        assert len(saved) == 1 and saved[0]['answer']['text'] == '对比学习 [1]'


# ── Token budget management in research_graph ────────────────────────

def test_graph_truncates_evidence_text_when_budget_exceeded():
    """Small context_window + large evidence → each text truncated, IDs preserved."""
    big_chunk = TextChunk(text='灯塔每隔十二秒闪白光。' * 100,
                          source_path='a.pdf', page_number=1, chunk_index=0)
    wf = ResearchWorkflowForTest(Library(), context_window=512, max_output_tokens=128)
    state = {'evidence': [big_chunk, big_chunk], 'question': '灯塔？', 'history': []}
    items = wf.context(state)
    assert len(items) == 2
    # IDs preserved (1-based sequential).
    assert items[0]['id'] == 1 and items[1]['id'] == 2
    # Each text was truncated (much shorter than original).
    assert len(items[0]['text']) < len(big_chunk.text)
    assert items[0]['text'].endswith('…')
    # Source metadata preserved.
    assert items[0]['source'] == 'a.pdf'
    assert items[0]['page'] == 1


def test_graph_small_context_preserves_full_evidence():
    """Default context_window + small evidence → no truncation."""
    small_chunk = TextChunk(text='灯塔闪白光。', source_path='a.pdf', page_number=1, chunk_index=0)
    wf = ResearchWorkflowForTest(Library())
    state = {'evidence': [small_chunk], 'question': '灯塔？', 'history': []}
    items = wf.context(state)
    assert items[0]['text'] == '灯塔闪白光。'
    # Chunks without page_spans keep an empty pages list (no synthetic excerpts).
    assert items[0]['pages'] == []


def test_graph_truncation_does_not_reinflate_via_page_excerpts():
    """When text is truncated, page excerpts must not restore the full original."""
    from deepsearch_agent.tokens import estimate_tokens
    original = '灯塔每隔十二秒闪白光。' * 100
    chunk = TextChunk(
        text=original,
        source_path='a.pdf',
        page_number=1,
        page_end=1,
        chunk_index=0,
        page_spans=[{'page_number': 1, 'start': 0, 'end': len(original),
                     'chunk_start': 0, 'chunk_end': len(original)}],
    )
    chunk.validate_structure()
    wf = ResearchWorkflowForTest(Library(), context_window=512, max_output_tokens=128)
    state = {'evidence': [chunk, chunk], 'question': '灯塔？', 'history': []}
    items = wf.context(state)
    budget = wf._evidence_budget(state)
    assert len(items[0]['text']) < len(original)
    # Truncated path keeps page numbers but not page bodies.
    assert items[0]['pages'] == [{'page_number': 1, 'text': ''}]
    assert all(p['text'] == '' for item in items for p in item['pages'])
    # Text+metadata should stay near the (floored) evidence budget.
    body_tokens = sum(estimate_tokens(item['text']) for item in items)
    assert body_tokens <= max(budget, 100)


def test_graph_evidence_budget_accounts_for_history_and_tasks():
    short = {'question': 'q', 'history': [], 'evidence': [TextChunk(text='x', source_path='a.pdf', page_number=1, chunk_index=0)]}
    long = {
        'question': 'q',
        'history': [{'role': 'user', 'content': '很长的历史。' * 80}] * 6,
        'tasks': [{'question': '子问题' * 40, 'status': 'pending'}] * 6,
        'claims': [{'text': '结论' * 50, 'citations': [1]}] * 6,
        'issues': ['问题说明' * 30] * 4,
        'missing_answerable': [{'question': '漏项' * 40, 'citations': [1]}] * 4,
        'evidence': short['evidence'],
    }
    wf = ResearchWorkflowForTest(Library())
    assert wf._evidence_budget(long) < wf._evidence_budget(short)


class ResearchWorkflowForTest:
    """Minimal stand-in to exercise context() without LangGraph."""
    def __init__(self, kb, context_window=8192, max_output_tokens=1024):
        self.kb = kb
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        from deepsearch_agent.research_graph import ResearchWorkflow
        self._rw = ResearchWorkflow.__new__(ResearchWorkflow)
        self._rw.kb = kb
        self._rw.context_window = context_window
        self._rw.max_output_tokens = max_output_tokens

    def context(self, state):
        return self._rw.context(state)

    def _evidence_budget(self, state):
        return self._rw._evidence_budget(state)


def test_graph_reranks_research_candidates():
    """research() uses reranker to over-fetch and reorder candidates."""
    from deepsearch_agent.research_graph import ResearchWorkflow

    class RerankingKB:
        def __init__(self):
            self.search_calls = []
        def search(self, query, top_k=5):
            self.search_calls.append(top_k)
            return [TextChunk(text=f'{query}-chunk-{i}', source_path='a.pdf', page_number=i+1, chunk_index=i)
                    for i in range(top_k)]

    class StubReranker:
        def __init__(self):
            self.calls = []
        def rerank(self, query, chunks, top_k):
            self.calls.append((query, len(chunks), top_k))
            return list(reversed(chunks))[:top_k]

    class FakeClient:
        def complete(self, prompt):
            return '{"queries": ["test"]}'

    kb = RerankingKB()
    reranker = StubReranker()
    wf = ResearchWorkflow(FakeClient(), kb, reranker=reranker)
    wf.research({'question': 'Q', 'history': [], 'tasks': [], 'instruction': 'go',
                 'queries': [], 'top_k': 3, 'rounds': 0, 'evidence': []})

    # Over-fetched (3 * 4 = 12, capped at 20 → 12)
    assert kb.search_calls[0] == 12
    # Reranker was called with 12 candidates, top_k=3
    assert reranker.calls[0][1] == 12
    assert reranker.calls[0][2] == 3
