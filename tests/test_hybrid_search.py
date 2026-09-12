import pytest
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.pdf import FakePdfReader


class Embeddings:
    def embed(self, texts):
        return [[1., 0.] for _ in texts]


def library(pages):
    kb = KnowledgeBase(FakePdfReader(pages), chunk_size=1000, overlap=0,
                       embedding_client=Embeddings(), embedding_id='test')
    kb.ingest_pdf('test.pdf')
    return kb


def test_bm25_promotes_exact_identifier_without_query_embedding():
    kb = library(['普通说明，没有编号。', '仪器型号 AX-247，采样频率为 100 Hz。'])
    kb.retrieval_mode = 'bm25'
    class NoRemote:
        def embed(self, texts):
            pytest.fail('BM25-only search must not call embedding service')
    kb.embedding_client = NoRemote()
    assert kb.search('AX-247', 1)[0].page_number == 2
    assert kb.search('unmentioned999', 3) == []


def test_hybrid_uses_keyword_signal_and_deduplicates():
    kb = library(['无关的普通文本。', '仪器型号 AX-247，采样频率为 100 Hz。'])
    kb.retrieval_mode = 'hybrid'
    result = kb.search('AX-247', 5)
    assert result[0].page_number == 2
    assert len(result) == 2


def test_chinese_terms_casefold_and_length_normalization():
    kb = library(['灯塔 ' * 30 + '历史 ' * 500, '北岬灯塔每隔十二秒闪一次白光。', 'BM25 supports SEARCH identifiers.'])
    kb.retrieval_mode = 'bm25'
    assert kb.search('十二秒 白光', 1)[0].page_number == 2
    assert kb.search('search', 1)[0].page_number == 3


def test_lexical_index_updates_after_append_and_load(tmp_path):
    kb = library(['普通文本。'])
    kb.retrieval_mode = 'bm25'
    assert kb.search('AX247') == []
    kb.pdf_reader = FakePdfReader(['AX247 is the new device.'])
    kb.ingest_pdf('new.pdf')
    assert kb.search('ax247', 1)[0].source_path == 'new.pdf'
    snapshot = tmp_path/'saved.json'
    kb.save(snapshot)
    restored = library(['irrelevant'])
    restored.retrieval_mode = 'bm25'
    restored.load(snapshot)
    assert restored.search('ax247', 1)[0].source_path == 'new.pdf'


def test_hybrid_no_lexical_match_preserves_semantic_results():
    kb = library(['first page', 'second page'])
    semantic = kb.search('unknown-term')
    kb.retrieval_mode = 'hybrid'
    assert kb.search('unknown-term') == semantic


def test_hybrid_fusion_uses_normalized_scores():
    """A chunk strong in both signals beats a chunk winning only one."""
    class Directed:
        def embed(self, texts):
            table = {
                'alpha direction': [1.0, 0.0],
                'semantics-zz9 record': [0.3, 0.954],
                'noise': [0.0, 1.0],
                'semantics-zz9': [1.0, 0.0],
            }
            return [table[text] for text in texts]

    kb = KnowledgeBase(
        FakePdfReader(['alpha direction', 'semantics-zz9 record', 'noise']),
        chunk_size=1000, overlap=0,
        embedding_client=Directed(), embedding_id='test',
    )
    kb.ingest_pdf('test.pdf')
    kb.retrieval_mode = 'hybrid'

    result = kb.search('semantics-zz9', 2)

    # Vector-only winner would be page 1; the page matching both the lexical
    # term and the vector direction must rank first after score fusion.
    assert [chunk.page_number for chunk in result] == [2, 1]


def test_semantic_search_switches_to_ann_for_large_corpus():
    """ANN threshold hit → IVF path still returns the true nearest chunks."""
    class Clustered:
        def embed(self, texts):
            table = {
                '甲甲': [1.0, 0.0, 0.0], '乙甲': [1.0, 0.0, 0.0],   # cluster A (pages 1-2)
                '丙丙': [0.0, 1.0, 0.0], '丁丙': [0.0, 1.0, 0.0],   # cluster B (pages 3-4)
                '戊戊': [0.0, 0.0, 1.0], '己戊': [0.0, 0.0, 1.0],   # cluster C (pages 5-6)
                '查询甲': [1.0, 0.0, 0.0],
            }
            return [table[text] for text in texts]

    kb = KnowledgeBase(
        FakePdfReader(['甲甲', '乙甲', '丙丙', '丁丙', '戊戊', '己戊']),
        chunk_size=1000, overlap=0,
        embedding_client=Clustered(), embedding_id='test',
        ann_threshold=4,
    )
    kb.ingest_pdf('test.pdf')

    result = kb.search('查询甲', 2)

    assert kb._ann is not None and kb._ann.is_approximate
    assert [chunk.page_number for chunk in result] == [1, 2]


def test_bm25_index_updates_incrementally_on_second_ingest():
    kb = library(['普通文本。'])
    kb.retrieval_mode = 'bm25'
    kb.search('普通')  # builds the cache over one chunk
    assert kb._bm25_count == 1

    kb.pdf_reader = FakePdfReader(['AX-247 device'])
    kb.ingest_pdf('new.pdf')
    assert kb.search('AX-247', 1)[0].source_path == 'new.pdf'

    # index extended without a full rebuild
    assert kb._bm25_count == 2
    assert kb._bm25_index is not None and len(kb._bm25_index) == 2
