import json
import pytest
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.pdf import FakePdfReader


class Embeddings:
    def __init__(self):
        self.inputs = []
    def embed(self, texts):
        self.inputs.extend(texts)
        return [[1., 0.] for _ in texts]


def make(pages, **kwargs):
    return KnowledgeBase(FakePdfReader(pages), embedding_client=Embeddings(), embedding_id='test',
                         chunking_strategy='section-rules-v1', **kwargs)


def test_sections_keep_hierarchy_and_do_not_mix_adjacent_sections():
    kb = make(['Abstract\nA summary.\n\n1 Introduction\nBackground.\n\n2 Methods\nOverview.\n\n2.1 Model\nA transformer.\n\n3 Results\nAccuracy increases.'])
    chunks = kb.ingest_pdf('paper.pdf')
    assert [c.section_path for c in chunks] == [['Abstract'], ['1 Introduction'], ['2 Methods'], ['2 Methods', '2.1 Model'], ['3 Results']]
    assert 'Results' not in chunks[3].text
    assert '2 Methods' in kb.embedding_client.inputs[3]
    kb.retrieval_mode = 'bm25'
    assert kb.search('transformer', 1)[0].section_path[-1] == '2.1 Model'


def test_cross_page_spans_reconstruct_exact_page_substrings():
    pages = ['2 Methods\nThe experiment uses', 'two GPUs.\n\n3 Results\nThe result is stable.']
    chunks = make(pages).ingest_pdf('paper.pdf')
    c = chunks[0]
    assert c.page_number == 1 and c.page_end == 2
    assert c.text == '2 Methods\nThe experiment uses\ntwo GPUs.'
    assert [s['page_number'] for s in c.page_spans] == [1, 2]
    for s in c.page_spans:
        assert c.text[s['chunk_start']:s['chunk_end']] == pages[s['page_number']-1][s['start']:s['end']]


def test_chinese_chapter_and_section_titles():
    chunks = make(['摘要\n本文研究分块。\n\n第三章 研究方法\n这里介绍方法。\n\n第二节 模型结构\n编码器和解码器。\n\n第四章 实验结果\n结果可靠。']).ingest_pdf('x.pdf')
    assert chunks[2].section_path == ['第三章 研究方法', '第二节 模型结构']
    assert chunks[3].section_path == ['第四章 实验结果']


def test_references_captions_toc_and_equations_are_not_sections():
    page = 'Contents\n1 Introduction .... 2\n2 Methods    5\n\nFigure 1. Architecture\nTable 2 Results\n[1] Smith et al. 2020\n1 + 2 = 3\n1. This is a complete sentence.\n这里是普通正文。'
    chunks = make([page]).ingest_pdf('x.pdf')
    assert all(not c.section_path for c in chunks)
    assert ''.join(c.text for c in chunks) == page


def test_no_headings_falls_back_to_page_aware_paragraphs():
    chunks = make(['First page.\n\nA paragraph.', 'Second page.']).ingest_pdf('x.pdf')
    assert len(chunks) == 2
    assert [c.page_number for c in chunks] == [1, 2]
    assert all(c.page_number == c.page_end for c in chunks)


def test_long_sections_respect_hard_limit_and_preserve_every_character():
    page = '1 Methods\n' + '甲' * 2300 + '\n\n2 Results\n结果。'
    chunks = make([page]).ingest_pdf('x.pdf')
    assert all(len(c.text) <= 900 for c in chunks)
    covered = set()
    for c in chunks:
        for s in c.page_spans:
            covered.update(range(s['start'], s['end']))
    assert all(i in covered for i, ch in enumerate(page) if not ch.isspace())
    assert chunks[-1].section_path == ['2 Results']


def test_section_snapshot_roundtrip_and_old_snapshot_compatibility(tmp_path):
    kb = make(['1 Methods\nOne page', 'and a second page.'])
    kb.ingest_pdf('x.pdf')
    path = tmp_path/'index.json'
    kb.save(path)
    restored = make([])
    restored.load(path)
    assert restored.chunks == kb.chunks
    assert restored.chunks[0].chunking['strategy'] == 'section-rules-v1'
    old = {'schema_version':1, 'embedding_id':'test', 'dimension':2, 'chunk_size':1000, 'overlap':200,
           'entries':[{'chunk':{'text':'old', 'source_path':'old.pdf', 'page_number':1, 'chunk_index':0}, 'vector':[1.,0.]}]}
    path.write_text(json.dumps(old), encoding='utf-8')
    restored.load(path)
    assert restored.chunks[0].text == 'old'
    assert not restored.chunks[0].section_path


def test_invalid_page_mapping_does_not_replace_current_index(tmp_path):
    kb = make(['1 Methods\nText.'])
    kb.ingest_pdf('x.pdf')
    path = tmp_path/'index.json'
    kb.save(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['entries'][0]['chunk']['page_spans'][0]['chunk_end'] = 9999
    path.write_text(json.dumps(data), encoding='utf-8')
    before = list(kb.chunks)
    with pytest.raises(ValueError):
        kb.load(path)
    assert kb.chunks == before


def test_sentence_overlap_keeps_complete_sentences():
    text = '1 Methods\n\n' + ' '.join(f'Sentence number {i} describes the method.' for i in range(15))
    chunks = make([text], chunk_size=100, overlap=45, max_chunk_size=140).ingest_pdf('x.pdf')
    assert len(chunks) > 2
    assert all(c.text.endswith('.') for c in chunks)
    assert 'Sentence number 1 describes the method.' in chunks[0].text
    assert 'Sentence number 1 describes the method.' in chunks[1].text
    assert all(len(c.text) <= 140 for c in chunks)


def test_answer_source_keeps_section_and_individual_page_evidence():
    from deepsearch_agent.qa import chunk_to_source_reference
    from deepsearch_agent.research_graph import ResearchWorkflow
    chunk = make(['2 Methods\nThe experiment uses', 'two GPUs.']).ingest_pdf('x.pdf')[0]
    source = chunk_to_source_reference(chunk)
    assert source.page_end == 2
    assert source.section_path == ['2 Methods']
    assert source.pages == [{'page_number':1, 'text':'2 Methods\nThe experiment uses'}, {'page_number':2, 'text':'two GPUs.'}]
    context = ResearchWorkflow(None, None).context({'evidence':[chunk]})
    assert context[0]['pages'] == source.pages
