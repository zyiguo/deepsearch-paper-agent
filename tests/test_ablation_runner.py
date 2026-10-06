from dataclasses import asdict
import json

import pytest

from deepsearch_agent.benchmarks import ablation as a
from deepsearch_agent.benchmarks.badcases import attribute_case
from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.qa import Answer, chunk_to_source_reference
from deepsearch_agent.vision_config import VisionSettings


def case(task='chartqa'):
    return {'id': 'q1', 'dataset': 'chartqa' if task == 'chartqa' else 'pdfvqa', 'task': task,
            'group': 'human', 'scope': ['p1'], 'gold_pages': ['p1'], 'answers': ['42'],
            'gold_element_ids': ['e1'] if task == 'B' else [], 'question': 'What value?'}


def test_parse_only_verified_single_claim():
    c = case()
    outcome = {'claims': [{'text': json.dumps({'answer': '42', 'element_ids': []})}], 'supported': [0]}
    assert a.parse_outcome(c, outcome, set())['answer'] == '42'
    assert a.parse_outcome(c, {**outcome, 'supported': []}, set())['prediction_status'] == 'abstained'
    assert a.parse_outcome(c, {**outcome, 'supported': [0, 0]}, set())['status'] == 'invalid_output'
    assert a.parse_outcome(c, {'status': 'schema_validation_failed'}, set())['status'] == 'invalid_output'


def test_unknown_elements_rejected_and_no_labels_in_format_adapter():
    c = case('B')
    outcome = {'claims': [{'text': '{"element_ids":["outside"]}'}], 'supported': [0]}
    assert a.parse_outcome(c, outcome, {'e1'})['status'] == 'invalid_output'
    assert 'gold' not in a.format_instruction('B')


def test_stratification_is_deterministic_and_answer_independent():
    cases = [{**case(), 'id': f'{g}{i}', 'group': g} for g in ('human', 'augmented') for i in range(5)]
    m = {'dataset': 'chartqa', 'cases': cases}
    selected = [c['id'] for c in a.selected_cases(m)]
    for c in cases:
        c['answers'] = ['unrelated']
    assert selected == [c['id'] for c in a.selected_cases(m)]
    assert len(selected) == len(set(selected)) == 6


def test_validate_composite_run_keys():
    rows = [{'id': 'q1', 'variant': v} for v in ('agent_full', 'oracle')]
    a.validate_rows(rows, [case()], a.VARIANTS)
    with pytest.raises(ValueError):
        a.validate_rows(rows + rows[:1], [case()], a.VARIANTS)


def test_predictor_accounts_for_real_chunk_record_fields(monkeypatch, tmp_path):
    c = case('B')
    chunk = TextChunk('ELEMENT {"id": "e1", "text": "42"}', 'paper', 1, 0, visual_pages=[1])
    kb = KnowledgeBase(None, retrieval_mode='bm25')
    kb.ingest_chunks([chunk])
    manifest = {'_root': tmp_path, 'pages': [{'id': 'p1', 'source': 'paper', 'page_number': 1,
        'image': 'not-used.png', 'text': chunk.text, 'elements': [{'id': 'e1'}]}]}
    class Client:
        def __init__(self, settings, trace):
            self.settings = settings
        def close(self):
            pass
    class Workflow:
        def __init__(self, *args, **kwargs):
            self.trace = kwargs['trace']
        def answer(self, *args):
            self.trace.observer('rag.outcome', {'status': 'verified',
                'claims': [{'text': '{"answer":"","element_ids":["e1"]}', 'citations': [1]}],
                'supported': [0], 'evidence': [asdict(chunk)]})
            return Answer('result', [chunk_to_source_reference(chunk)])
    monkeypatch.setattr(a, 'MeasuredClient', Client)
    monkeypatch.setattr(a, 'BenchmarkWorkflow', Workflow)
    vision = VisionSettings('openai_compatible', LLMSettings(backend='openai_compatible',
        base_url='https://example.test/v1', api_key='fake', model='vision'))
    row = a.predict_case(c, 'fixed_workflow', manifest, kb, vision, a.AgentSettings(), 3, 18)
    assert row['status'] == 'ok', row
    assert row['evidence_ranking'] == ['p1']
    assert row['evidence_element_ids'] == ['e1']


def test_production_rerank_status_attribution():
    row = {'id': 'q1', 'variant': 'agent_full', 'answer': '0', 'status': 'ok',
           'ranking': ['other'], 'evidence_ranking': ['other'], 'trace': {
               'rerank_calls': [{'status': 'ok_multimodal', 'initial_ranking': ['p1', 'other'],
                                 'ranking': ['other']}]}}
    assert attribute_case(case(), row)['primary_code'] == 'rerank_drop'


def test_ablation_tool_reader_and_ocr_catalog():
    from deepsearch_agent.benchmarks.instrumentation import RunTrace
    kb = KnowledgeBase(None, retrieval_mode='bm25')
    kb.ingest_chunks([TextChunk('text', 'paper', 1, 0)])
    session = a.AblationSession(kb, trace=RunTrace({('paper', 1): 'p1'}),
        page_reader=lambda source, page: {'text': 'full page'}, ocr_enabled=False)
    assert session.invoke('read_page', {'source': 'paper', 'page': 1}).chunks[0].text == 'full page'
    assert 'ocr_page' not in {s['name'] for s in session.specs()}
    assert 'ocr_page' not in {t.name for t in session.tools()}
