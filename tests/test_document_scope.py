import pytest
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.pdf import FakePdfReader


class Embeddings:
    def embed(self, texts):
        return [[1., 0.] for _ in texts]


@pytest.mark.parametrize('mode', ['semantic', 'bm25', 'hybrid'])
def test_scope_filters_before_top_k_and_does_not_mutate_index(mode):
    kb = KnowledgeBase(FakePdfReader(['GPU two']), embedding_client=Embeddings(), retrieval_mode=mode)
    for i in range(25):
        kb.ingest_pdf(f'demo://{i}')
    kb.ingest_pdf('pdf://selected/paper.pdf')
    scoped = kb.restrict_sources({'pdf://selected/paper.pdf'})
    assert [c.source_path for c in scoped.search('GPU', top_k=1)] == ['pdf://selected/paper.pdf']
    assert len(kb.chunks) == 26
    assert len(kb._vectors) == 26


def test_empty_scope_never_searches_other_documents_or_calls_embedding():
    kb = KnowledgeBase(FakePdfReader(['GPU']), embedding_client=Embeddings())
    kb.ingest_pdf('demo://example.pdf')
    class NoCalls:
        def embed(self, texts):
            pytest.fail('empty scope must not call embedding')
    kb.embedding_client = NoCalls()
    assert kb.restrict_sources(set()).search('GPU') == []


def test_lexical_cache_does_not_leak_between_scopes():
    kb = KnowledgeBase(FakePdfReader(['GPU']), retrieval_mode='bm25')
    kb.ingest_pdf('a.pdf'); kb.ingest_pdf('b.pdf')
    kb.search('GPU')
    assert kb.restrict_sources({'b.pdf'}).search('GPU')[0].source_path == 'b.pdf'


def test_graph_receives_only_selected_evidence():
    import json
    from deepsearch_experiments.legacy.qa import AnswerService
    from test_supervisor import Script, decision
    kb = KnowledgeBase(FakePdfReader(['GPU white']), embedding_client=Embeddings())
    kb.ingest_pdf('demo://example.pdf')
    kb.ingest_pdf('selected.pdf')
    class CheckedScript(Script):
        def complete(self, prompt):
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            if data.get('evidence'):
                assert {item['source'] for item in data['evidence']} == {'selected.pdf'}
            return super().complete(prompt)
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['GPU']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims': [{'text': 'white', 'citations': [1]}]}),
             ('Verifier', {'supported': [0], 'queries': []}), ('Supervisor', decision('finish'))]
    result = AnswerService(CheckedScript(steps), kb, use_graph=True).answer('GPU', allowed_sources={'selected.pdf'})
    assert [s.source_path for s in result.sources] == ['selected.pdf']


@pytest.mark.parametrize('suffix', ['/turns', '/turns/stream'])
def test_api_rejects_empty_or_unknown_scope_before_model(tmp_path, suffix):
    from fastapi.testclient import TestClient
    from test_streaming import app_for
    app = app_for(tmp_path)
    class NoCalls:
        def answer(self, **kwargs):
            pytest.fail('invalid scope reached model')
    app.state.answer_service = NoCalls()
    with TestClient(app) as client:
        sid = client.post('/api/sessions', json={'title': 'scope'}).json()['id']
        for ids in ([], ['b' * 64], ['demo://example.pdf']):
            result = client.post(f'/api/sessions/{sid}{suffix}', json={'question': 'GPU', 'document_ids': ids})
            assert result.status_code == 422


@pytest.mark.parametrize('ids', [None, ['a' * 64]])
def test_real_api_resolves_pdf_scope_and_excludes_demo(tmp_path, ids):
    from fastapi.testclient import TestClient
    from test_streaming import app_for
    from deepsearch_agent.core.evidence import TextChunk
    from deepsearch_agent.core.answers import Answer
    app = app_for(tmp_path)
    app.state.kb.chunks.append(TextChunk(text='GPU', source_path='demo://example.pdf', page_number=1, chunk_index=0))
    class CheckedService:
        def answer(self, **kwargs):
            assert kwargs['allowed_sources'] == {'pdf://' + 'a' * 64 + '/fixture.pdf'}
            return Answer('scope enforced', [])
    app.state.answer_service = CheckedService()
    with TestClient(app) as client:
        sid = client.post('/api/sessions', json={'title': 'scope'}).json()['id']
        result = client.post(f'/api/sessions/{sid}/turns', json={'question': 'GPU', 'document_ids': ids})
        assert result.status_code == 201
