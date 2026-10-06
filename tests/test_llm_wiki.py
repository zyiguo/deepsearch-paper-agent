from types import SimpleNamespace

import pytest

from deepsearch_agent.llm_wiki import WikiError, WikiPage, WikiSource, WikiStore, WikiView, WikiCompiler


def page(identifier='wiki_' + 'a' * 32, chunk='chk_' + 'a' * 64, source='a.pdf'):
    return WikiPage(id=identifier, title='Light signals', markdown='White light every 12 seconds. [1]',
                    sources=[WikiSource(chunk_id=chunk, source_path=source, page_number=1)],
                    updated_at='2026-10-06T00:00:00+00:00')


def test_wiki_markdown_persists_and_requires_revision_for_update(tmp_path):
    store = WikiStore(tmp_path)
    saved = store.save(page())
    assert (tmp_path / (saved.id + '.md')).exists()
    assert WikiStore(tmp_path).get(saved.id) == saved
    with pytest.raises(WikiError, match='wiki_conflict'):
        store.save(page())
    updated = page().model_copy(update={'markdown': 'Rechecked light. [1]'})
    newer = store.save(updated, expected_revision=saved.revision)
    assert newer.revision != saved.revision
    with pytest.raises(WikiError, match='wiki_conflict'):
        store.save(page(), expected_revision=saved.revision)


def test_wiki_scope_and_stale_sources_are_not_exposed(tmp_path):
    store = WikiStore(tmp_path)
    saved = store.save(page())
    registry = {saved.sources[0].chunk_id: SimpleNamespace(source_path='a.pdf')}
    assert WikiView(store, {'a.pdf'}, registry).search('light')[0]['id'] == saved.id
    assert WikiView(store, {'b.pdf'}, registry).search('light') == []
    assert WikiView(store, {'a.pdf'}, {}).search('light') == []
    with pytest.raises(WikiError, match='wiki_not_available'):
        WikiView(store, {'b.pdf'}, registry).read(saved.id)
    assert 'stale_source' in {issue['code'] for issue in store.lint({})}


def test_wiki_search_reuses_chinese_bm25_tokenization(tmp_path):
    store = WikiStore(tmp_path)
    saved = store.save(page().model_copy(update={'title': '对比学习', 'markdown': '模型使用对比学习训练。 [1]'}))
    registry = {saved.sources[0].chunk_id: SimpleNamespace(source_path='a.pdf')}
    assert WikiView(store, {'a.pdf'}, registry).search('如何理解对比学习')[0]['id'] == saved.id


def test_wiki_rejects_paths_and_checks_delete_version(tmp_path):
    store = WikiStore(tmp_path)
    with pytest.raises(WikiError, match='wiki_invalid_id'):
        store.get('../secret')
    saved = store.save(page())
    with pytest.raises(WikiError, match='wiki_conflict'):
        store.delete(saved.id, 'b' * 64)
    store.delete(saved.id, saved.revision)
    assert store.list_pages() == []


def test_wiki_compiler_uses_real_workflow_and_rejects_unverified(tmp_path):
    from test_research_graph import Client, Library
    from deepsearch_agent.qa import AnswerService
    outputs = [{'queries': ['light']}, {'claims': [{'text': 'white light', 'citations': [1]}]},
               {'supported': [0], 'queries': []}]
    kb = Library()
    kb.restrict_sources = lambda sources: kb
    service = AnswerService(Client(outputs), kb, use_graph=True)
    compiled = WikiCompiler(service).compile('Light', 'signal?', {'a.pdf'})
    assert compiled.markdown == 'white light [1]'
    assert compiled.sources[0].source_path == 'a.pdf'
    service = AnswerService(Client([{'queries': ['light']}, {'claims': []},
                                   {'supported': [], 'queries': []}]), kb, use_graph=True)
    with pytest.raises(WikiError, match='wiki_unverified'):
        WikiCompiler(service).compile('Light', 'signal?', {'a.pdf'})


def test_wiki_tools_are_navigation_not_citation_evidence(tmp_path):
    from deepsearch_agent.chunking import TextChunk
    from deepsearch_agent.tool_service import PaperToolSession
    chunk = TextChunk('White light', 'a.pdf', 1, 0)
    store = WikiStore(tmp_path)
    saved = store.save(page(chunk=chunk.chunk_id))
    kb = SimpleNamespace(chunks=[chunk], image_index=None)
    session = PaperToolSession(kb, wiki_store=store)
    result = session.invoke('read_wiki', {'page_id': saved.id})
    assert result.data['trust'] == 'derived_navigation_not_evidence'
    assert session.evidence == []
    assert result.data['sources'][0]['chunk_id'] == chunk.chunk_id
    assert session.invoke('get_evidence', {'chunk_id': chunk.chunk_id}).chunks == [chunk]


def test_wiki_api_compile_link_update_and_delete(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.chunking import TextChunk
    from deepsearch_agent.llm_config import LLMSettings
    from uuid import uuid4
    source = 'pdf://' + 'b' * 64 + '/paper.pdf'
    chunk = TextChunk('White light', source, 1, 0)
    def compile(self, title, question, sources, *, page_id=None, top_k=5):
        assert sources == {source}
        return page(page_id or 'wiki_' + uuid4().hex, chunk.chunk_id, source)
    monkeypatch.setattr(WikiCompiler, 'compile', compile)
    app = create_app(tmp_path / 'index.json', llm_settings=LLMSettings(backend='demo'))
    with TestClient(app) as client:
        app.state.kb.embedding_client = None
        app.state.kb.chunks = [chunk]
        body = {'title': 'Light', 'question': 'signal?', 'document_ids': ['b' * 64]}
        first = client.post('/api/wiki/compile', json=body)
        assert first.status_code == 201
        first = first.json()
        second = client.post('/api/wiki/compile', json=body).json()
        assert second['related'] == [first['id']]
        assert second['id'] in client.get('/api/wiki/' + first['id']).json()['backlinks']
        assert first['id'] in client.get('/api/wiki/index').text
        assert client.get('/api/wiki?query=light').json()
        assert client.post('/api/wiki/compile', json={**body, 'document_ids': ['c' * 64]}).status_code == 422
        assert client.post('/api/wiki/compile', json={**body, 'page_id': first['id'],
            'expected_revision': 'c' * 64}).status_code == 409
        assert client.delete('/api/wiki/' + first['id'], params={
            'expected_revision': first['revision']}).status_code == 200
        issues = client.get('/api/wiki/lint').json()['issues']
        assert any(issue['code'] == 'broken_link' for issue in issues)
        app.state.kb.chunks = []
        assert client.get('/api/wiki').json() == []
        assert client.get('/api/wiki/' + second['id']).status_code == 404


def test_wiki_compile_rechecks_sources_after_model_finishes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.chunking import TextChunk
    from deepsearch_agent.llm_config import LLMSettings
    source = 'pdf://' + 'b' * 64 + '/paper.pdf'
    chunk = TextChunk('White light', source, 1, 0)
    app = create_app(tmp_path / 'index.json', llm_settings=LLMSettings(backend='demo'))
    def compile(*args, **kwargs):
        app.state.kb.chunks.clear()
        return page(chunk=chunk.chunk_id, source=source)
    monkeypatch.setattr(WikiCompiler, 'compile', compile)
    with TestClient(app) as client:
        app.state.kb.embedding_client = None
        app.state.kb.chunks = [chunk]
        response = client.post('/api/wiki/compile', json={
            'title': 'Light', 'question': 'signal?', 'document_ids': ['b' * 64]})
        assert response.status_code == 409
        assert app.state.wiki.list_pages() == []
