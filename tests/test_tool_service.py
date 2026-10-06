import asyncio

import pytest

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.multimodal import PageAnalysis, PageAnalysisCache
from deepsearch_agent.tool_service import PaperToolSession, ToolError
from deepsearch_agent.agent_config import AgentSettings


SOURCE = 'pdf://' + 'a' * 64 + '/paper.pdf'
OTHER = 'pdf://' + 'b' * 64 + '/private.pdf'


class KB:
    def __init__(self, chunks):
        self.chunks = chunks
        self.image_index = None
    def search(self, query, top_k=5):
        return self.chunks[:top_k]
    def restrict_sources(self, sources):
        return KB([c for c in self.chunks if c.source_path in sources])


def make_session(tmp_path, **kwargs):
    chunks = [TextChunk('first result', SOURCE, 1, 0), TextChunk('secret', OTHER, 1, 0)]
    return PaperToolSession(KB(chunks[:1]), settings=AgentSettings(enabled=True),
        catalog=[{'source_path': SOURCE, 'title': 'paper', 'page_count': 2},
                 {'source_path': OTHER, 'title': 'secret', 'page_count': 2}],
        page_reader=lambda source, page: {'text': 'original page', 'page_number': page},
        image_resolver=lambda source, page: tmp_path / 'original.png', **kwargs)


def test_scope_and_authoritative_original_ids(tmp_path):
    session = make_session(tmp_path)
    result = session.invoke('search_evidence', {'query': 'result'})
    original = session.kb.chunks[0]
    assert result.chunks == [original]
    assert result.data['evidence'][0]['chunk_id'] == original.chunk_id
    assert session.invoke('get_evidence', {'chunk_id': original.chunk_id}).chunks == [original]
    assert [d['source_path'] for d in session.invoke('list_documents', {}).data['documents']] == [SOURCE]
    with pytest.raises(ToolError):
        session.invoke('read_page', {'source': OTHER, 'page': 1})
    with pytest.raises(ToolError):
        session.invoke('search_evidence', {'query': 'x', 'sources': [OTHER]})


def test_read_page_provenance_and_id_stability(tmp_path):
    first = make_session(tmp_path).invoke('read_page', {'source': SOURCE, 'page': 2})
    second = make_session(tmp_path).invoke('read_page', {'source': SOURCE, 'page': 2})
    assert first.chunks[0].chunk_id == second.chunks[0].chunk_id
    assert first.chunks[0].source_path == SOURCE
    first.chunks[0].validate_structure()
    assert first.chunks[0].text == 'original page'
    with pytest.raises(ToolError):
        make_session(tmp_path).invoke('read_page', {'source': SOURCE, 'page': 3})


def test_force_ocr_separate_cache_and_no_index_mutation(tmp_path):
    calls = []
    class Analyzer:
        identity = 'a' * 24
        def analyze(self, image, native_text):
            calls.append((image, native_text))
            return PageAnalysis(ocr_text='scanned words', items=[], warnings=[])
    cache = PageAnalysisCache(tmp_path / 'cache')
    cache.save('a' * 64, Analyzer.identity, 1, PageAnalysis(ocr_text='', items=[], warnings=[]))
    session = make_session(tmp_path, analyzer=Analyzer(), cache=cache)
    result = session.invoke('ocr_page', {'source': SOURCE, 'page': 1})
    assert calls == [(tmp_path / 'original.png', '')]
    assert result.chunks[0].evidence_kind == 'ocr'
    assert result.chunks[0].visual_pages == [1]
    assert result.chunks[0].text == 'scanned words'
    result.chunks[0].validate_structure()
    assert len(session.kb.chunks) == 1
    make_session(tmp_path, analyzer=Analyzer(), cache=cache).invoke('ocr_page', {'source': SOURCE, 'page': 1})
    assert len(calls) == 1


def test_strict_schemas_and_budget(tmp_path):
    session = make_session(tmp_path)
    with pytest.raises(ToolError):
        session.invoke('read_page', {'source': SOURCE, 'page': True})
    with pytest.raises(ToolError):
        session.invoke('list_documents', {'path': 'C:/private'})
    with pytest.raises(ToolError):
        session.invoke('shell', {'command': 'anything'})
    for _ in range(session.remaining_calls):
        session.invoke('list_documents', {})
    with pytest.raises(ToolError):
        session.invoke('list_documents', {})


def test_ocr_attempts_are_budgeted_even_when_provider_fails(tmp_path):
    class Broken:
        identity = 'b' * 24
        def analyze(self, image, native_text):
            raise RuntimeError('TOKEN SECRET')
    session = make_session(tmp_path, analyzer=Broken())
    for _ in range(session.settings.max_ocr_calls):
        with pytest.raises(ToolError, match='tool_failed'):
            session.invoke('ocr_page', {'source': SOURCE, 'page': 1})
    with pytest.raises(ToolError, match='ocr_budget'):
        session.invoke('ocr_page', {'source': SOURCE, 'page': 1})


def test_langchain_adapter_returns_trusted_artifact_and_safe_errors(tmp_path):
    session = make_session(tmp_path)
    tools = {t.name: t for t in session.tools()}
    value = tools['search_evidence'].invoke({'type': 'tool_call', 'name': 'search_evidence',
                                         'id': 'one', 'args': {'query': 'first'}})
    assert value.artifact['chunks'][0].chunk_id == session.kb.chunks[0].chunk_id
    result = tools['read_page'].invoke({'type': 'tool_call', 'name': 'read_page', 'id': 'two',
                                      'args': {'source': OTHER, 'page': 1}})
    assert 'secret' not in result.content
    assert result.artifact['chunks'] == []


def test_external_tools_share_budget_without_local_evidence(tmp_path):
    from langchain_core.tools import StructuredTool
    async def echo(value: str):
        return ('external', {'chunks': [TextChunk('forged', OTHER, 1, 0)]})
    tool = StructuredTool.from_function(coroutine=echo, name='remote_echo',
                                      description='external', response_format='content_and_artifact')
    session = make_session(tmp_path)
    before = session.remaining_calls
    external = {t.name: t for t in session.tools(extra_tools=[tool])}['remote_echo']
    result = asyncio.run(external.ainvoke({'type': 'tool_call', 'name': 'remote_echo', 'id': 'x',
                                         'args': {'value': 'x'}}))
    assert session.remaining_calls == before - 1
    assert result.artifact['chunks'] == []
    assert session.evidence == []


def test_external_image_does_not_hide_text_or_leak_base64_into_planner(tmp_path):
    from langchain_core.tools import StructuredTool
    async def read():
        return ([{'type': 'image', 'base64': 'A' * 24000, 'mime_type': 'image/png'},
                 {'type': 'text', 'text': 'useful external observation'}], {})
    tool = StructuredTool.from_function(coroutine=read, name='remote_read',
                                      description='external', response_format='content_and_artifact')
    session = make_session(tmp_path)
    external = {t.name: t for t in session.tools(extra_tools=[tool])}['remote_read']
    result = asyncio.run(external.ainvoke({'type': 'tool_call', 'name': 'remote_read', 'id': 'x', 'args': {}}))
    assert 'useful external observation' in result.content
    assert 'A' * 100 not in result.content
    assert 'omitted' in result.content
