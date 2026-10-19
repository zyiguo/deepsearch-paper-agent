import json
import pickle
from pathlib import Path

import pytest

from deepsearch_experiments.benchmarks.metrics import relaxed_correctness, element_scores, ranking_scores
from deepsearch_experiments.benchmarks.datasets import chartqa_cases, pdfvqa_page_cases, primitive_pickle
from deepsearch_experiments.benchmarks.schema import load_manifest
from deepsearch_experiments.benchmarks.runner import run_cases, summarize
from deepsearch_experiments.benchmarks.runtime import answer_prompt


@pytest.mark.parametrize('gold,pred,expected', [
    ('100', '104', True), ('100', '106', False), ('50%', '0.5', True),
    ('0', '0.0', False), ('-100', '-104', True), ('Blue', 'blue', True),
    ('100', 'the answer is 100', False), ('nan', '100', False),
])
def test_chartqa_reference_metric(gold, pred, expected):
    assert relaxed_correctness(gold, pred) is expected


def test_multi_element_answers_are_sets_not_alternative_answers():
    assert element_scores(['1', '2'], ['1']) == {'element_exact_match': 0., 'element_f1': 2 / 3}
    assert element_scores(['1', '2'], ['2', '1'])['element_exact_match'] == 1.
    assert ranking_scores(['p1', 'p1', 'p2'], ['p2'], 2)['mrr'] == .5


def test_chart_adapter_does_not_index_questions_or_answers():
    cases = chartqa_cases([{'imgname': 'a.png', 'query': 'Secret question', 'label': ['42']}], 'val', 'human')
    assert cases[0]['answers'] == ['42']
    with pytest.raises(ValueError):
        chartqa_cases([{'imgname': '../a.png', 'query': 'q', 'label': ['1']}], 'val', 'human')


def test_chartqa_bracketed_answer_is_literal_text_not_python():
    result = chartqa_cases([{'imgname': 'a.png', 'query': 'q', 'label': '[Madagascar, Central Europe]'}], 'val', 'human')
    assert result[0]['answers'] == ['[Madagascar, Central Europe]']


def page_row():
    return {'object_ids': ['10', '11'], 'texts': ['Introduction', 'Results'],
            'bboxes': [[0, 0, 20, 10], [0, 20, 20, 10]],
            'questions': ['Is there a figure?', 'What is the bottom section about?'],
            'answers': ['False', 'Results'], 'types': ['existence', 'structural_understanding'],
            'task types': ['A', 'B']}


def test_pdf_mirror_normalizes_boolean_and_maps_only_unique_elements():
    cases, skipped = pdfvqa_page_cases(page_row(), 7, 'validation')
    assert cases[0]['answers'] == ['no']
    assert cases[1]['gold_element_ids'] == ['11'] and skipped == 0
    row = page_row()
    row['texts'] = ['Results', 'Results']
    cases, skipped = pdfvqa_page_cases(row, 7, 'validation')
    assert len(cases) == 1 and skipped == 1


def test_pdf_missing_ocr_is_not_a_literal_none_answer():
    row = page_row()
    row['texts'][0] = None
    row['answers'][1] = None
    cases, skipped = pdfvqa_page_cases(row, 7, 'validation')
    assert len(cases) == 1 and skipped == 1


def test_pickle_import_never_executes_globals():
    assert primitive_pickle(pickle.dumps({'pages': {0: {'name': 'page.png'}}}))['pages'][0]['name'] == 'page.png'
    with pytest.raises(ValueError, match='executable'):
        primitive_pickle(pickle.dumps(Path('unsafe')))


def manifest(tmp_path):
    from PIL import Image
    Image.new('RGB', (100, 100), 'white').save(tmp_path / 'page.png')
    import hashlib
    data = {'schema_version': 1, 'dataset': 'chartqa', 'split': 'val', 'provenance': {'status': 'unit-fixture'},
            'pages': [{'id': 'p1', 'source': 'benchmark://fixture/1', 'page_number': 1,
                       'image': 'page.png', 'image_sha256': hashlib.sha256((tmp_path / 'page.png').read_bytes()).hexdigest(),
                       'text': '', 'elements': []}],
            'cases': [{'id': 'q1', 'dataset': 'chartqa', 'split': 'val', 'task': 'chartqa',
                       'group': 'human', 'question': 'Count?', 'answers': ['1'], 'gold_element_ids': [],
                       'scope': ['p1'], 'gold_pages': ['p1']}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(data), encoding='utf-8')
    return load_manifest(tmp_path / 'manifest.json')


def test_manifest_rejects_path_escape_and_asset_changes(tmp_path):
    m = manifest(tmp_path)
    assert m['pages'][0]['id'] == 'p1'
    p = tmp_path / 'manifest.json'
    data = json.loads(p.read_text())
    data['pages'][0]['image'] = '../outside.png'
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_manifest(p)


def test_prompt_never_includes_gold_labels_or_relation_graph(tmp_path):
    m = manifest(tmp_path)
    case = {**m['cases'][0], 'answers': ['SECRET_GOLD'], 'gold_element_ids': ['SECRET_ID']}
    prompt = answer_prompt(case, m['pages'])
    assert 'SECRET_GOLD' not in prompt and 'SECRET_ID' not in prompt


def test_failures_count_zero_and_resume_rejects_configuration_change(tmp_path):
    m = manifest(tmp_path)
    calls = []
    def fail(case):
        calls.append(case['id'])
        raise RuntimeError('do not expose secrets in exception messages')
    output = tmp_path / 'run'
    report = run_cases(m, fail, output, {'mode': 'oracle', 'model': 'test'}, workers=1)
    assert report['summary']['count'] == 1 and report['summary']['error_count'] == 1
    assert report['summary']['relaxed_accuracy'] == 0.
    assert 'do not expose' not in (output / 'predictions.jsonl').read_text()
    run_cases(m, fail, output, {'mode': 'oracle', 'model': 'test'}, resume=True)
    assert calls == ['q1']
    with pytest.raises(ValueError, match='identity'):
        run_cases(m, fail, output, {'mode': 'oracle', 'model': 'different'}, resume=True)


def test_summary_keeps_missing_predictions_in_denominator(tmp_path):
    m = manifest(tmp_path)
    result = summarize(m['cases'], [])
    assert result['count'] == 1 and result['missing_count'] == 1 and result['relaxed_accuracy'] == 0.


def test_download_retries_transient_gateway_failure_without_changing_data(monkeypatch):
    import httpx
    from deepsearch_experiments.benchmarks.downloads import fetch
    monkeypatch.setattr('deepsearch_experiments.benchmarks.downloads.time.sleep', lambda seconds: None)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(502 if len(calls) == 1 else 200, content=b'original')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch(client, 'https://raw.githubusercontent.com/official/file') == b'original'
    assert len(calls) == 2


def test_invalid_model_output_remains_failure_with_raw_output(tmp_path):
    m = manifest(tmp_path)
    report = run_cases(m, lambda case: {'status': 'invalid_output', 'raw_output': 'invalid'},
                       tmp_path / 'invalid-run', {'mode': 'oracle'})
    assert report['summary']['error_count'] == 1 and report['summary']['relaxed_accuracy'] == 0.
    assert 'invalid' in (tmp_path / 'invalid-run' / 'predictions.jsonl').read_text()


def test_rerank_metrics_do_not_invent_answer_scores_and_missing_ranks_count_zero(tmp_path):
    m = manifest(tmp_path)
    result = summarize(m['cases'], [], k=1, mode='rerank')
    assert 'relaxed_accuracy' not in result and result['recall@1'] == 0.


def test_original_pdf_task_c_preserves_full_document_scope_without_relations():
    from deepsearch_experiments.benchmarks.datasets import pdfvqa_document_cases
    layout = {'doc': {'pages': {
        0: {'name': 'first.png', 'objects': {'0': {'global_id': 10, 'text': 'Intro', 'bbox': [0, 0, 2, 2], 'relations': ['SECRET']}}},
        1: {'name': 'second.png', 'objects': {'0': {'global_id': 11, 'text': 'Results', 'bbox': [0, 0, 2, 2]}}}}}}
    pages, cases = pdfvqa_document_cases([{'pmcid': 'doc', 'question': 'Sections?', 'global_id': '[10,11]',
                                         'answer': "['Intro','Results']"}], layout, 'C', 'val')
    assert len(cases[0]['scope']) == 2 and len(cases[0]['gold_pages']) == 2
    assert cases[0]['gold_element_ids'] == ['10', '11']
    assert 'SECRET' not in answer_prompt(cases[0], pages)
    with pytest.raises(ValueError, match='Unlabelled'):
        pdfvqa_document_cases([{'pmcid': 'doc', 'question': 'Sections?'}], layout, 'C', 'val')


def test_bm25_index_is_isolated_and_roundtrips_without_vectors(tmp_path):
    from deepsearch_experiments.benchmarks.runtime import build_index, load_index, save_index
    m = manifest(tmp_path)
    m['pages'][0]['text'] = 'scientific chart evidence'
    kb = build_index(m)
    index = tmp_path / 'index.json'
    save_index(kb, m, index, {'retrieval': 'bm25'})
    restored = load_index(index, m)
    assert restored.search('scientific')[0].chunk_id == kb.chunks[0].chunk_id
    assert restored.image_index is None and restored._vectors == []


def test_scalar_prediction_can_omit_inapplicable_element_ids(tmp_path):
    from deepsearch_experiments.benchmarks.runtime import BenchmarkPredictor
    from deepsearch_agent.adapters.vision_config import VisionSettings
    from deepsearch_agent.adapters.llm_config import LLMSettings
    class Client:
        def complete_images(self, prompt, images, **kwargs):
            return '{"answer":"1"}'
    m = manifest(tmp_path)
    settings = VisionSettings(llm=LLMSettings(context_window=16000))
    result = BenchmarkPredictor(m, Client(), settings)(m['cases'][0])
    assert result.get('status', 'ok') == 'ok' and result['answer'] == '1'


@pytest.mark.parametrize('task,label', [('A', ''), ('A', None), ('A', '6'),
                                      ('B', None), ('B', ''), ('C', None), ('C', ' ')])
def test_original_pdf_rejects_unlabelled_or_invalid_gold(task, label):
    from deepsearch_experiments.benchmarks.datasets import pdfvqa_document_cases
    layout = {'doc': {'pages': {0: {'name': 'page.png', 'objects': {
        '0': {'global_id': 10, 'text': 'Intro', 'bbox': [0, 0, 2, 2]}}}}}}
    row = {'pmcid': 'doc', 'question': 'Sections?', 'page_index': 0}
    row['answer' if task == 'A' else 'global_id'] = label
    with pytest.raises(ValueError):
        pdfvqa_document_cases([row], layout, task, 'val')


def test_original_pdf_distinguishes_explicit_empty_gold_from_missing_label():
    from deepsearch_experiments.benchmarks.datasets import pdfvqa_document_cases
    layout = {'doc': {'pages': {0: {'name': 'page.png', 'objects': {}}}}}
    _, cases = pdfvqa_document_cases([{'pmcid': 'doc', 'question': 'Sections?', 'global_id': []}], layout, 'C', 'val')
    assert cases[0]['gold_element_ids'] == []


@pytest.mark.parametrize('task,change', [('chartqa', {'answers': [' ']}), ('A', {'answers': ['']}),
                                       ('A', {'answers': ['6']}), ('C', {'gold_element_ids': None})])
def test_manifest_rejects_empty_or_invalid_gold(tmp_path, task, change):
    manifest(tmp_path)
    path = tmp_path / 'manifest.json'
    data = json.loads(path.read_text())
    data['dataset'] = 'chartqa' if task == 'chartqa' else 'pdfvqa'
    data['cases'][0].update(dataset=data['dataset'], task=task, **change)
    if task == 'C':
        del data['cases'][0]['gold_element_ids']
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_manifest(path)


@pytest.mark.parametrize('prediction', [{'element_ids': '1'}, {}, {'element_ids': [1]},
                                       {'element_ids': ['1', '1']}])
def test_offline_element_scoring_rejects_invalid_predictions(prediction):
    case = {'id': 'q', 'task': 'C', 'answers': [], 'gold_element_ids': [], 'gold_pages': []}
    result = summarize([case], [{'id': 'q', 'status': 'ok', **prediction}], mode='oracle')
    assert result['error_count'] == 1 and result['element_exact_match'] == 0. and result['element_f1'] == 0.


def test_offline_scalar_scoring_requires_answer_and_ranking_lists(tmp_path):
    m = manifest(tmp_path)
    result = summarize(m['cases'], [{'id': 'q1', 'status': 'ok'}], mode='oracle')
    assert result['error_count'] == 1
    case = {**m['cases'][0], 'gold_pages': ['1']}
    result = summarize([case], [{'id': 'q1', 'status': 'ok', 'ranking': '1'}], mode='rerank')
    assert result['error_count'] == 1 and result['recall@5'] == 0.


def test_pdf_rag_scopes_original_document_by_page_not_only_source(tmp_path):
    from deepsearch_experiments.benchmarks.runtime import BenchmarkPredictor, build_index
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.vision_config import VisionSettings
    m = manifest(tmp_path)
    m['pages'][0]['text'] = 'scientific first page'
    m['pages'].append({**m['pages'][0], 'id': 'p2', 'page_number': 2, 'text': 'scientific second page'})
    m['cases'][0].update(dataset='pdfvqa', task='A', question='scientific', answers=['yes'])
    images_seen = []
    class Client:
        def complete_images(self, prompt, images, **kwargs):
            images_seen.append(len(images))
            return '{"answer":"yes","element_ids":[]}'
    kb = build_index(m)
    settings = VisionSettings(llm=LLMSettings(context_window=32000))
    result = BenchmarkPredictor(m, Client(), settings, mode='rag', knowledge_base=kb)(m['cases'][0])
    assert result['ranking'] == ['p1'] and images_seen == [1]
    assert len(kb.chunks) == 2


def test_page_scope_preserves_text_and_image_vector_alignment(tmp_path):
    from deepsearch_experiments.benchmarks.runtime import scoped_index
    from deepsearch_agent.core.evidence import TextChunk
    from deepsearch_agent.knowledge.image_index import ImageVectorIndex
    from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
    m = manifest(tmp_path)
    source = m['pages'][0]['source']
    kb = KnowledgeBase(None, embedding_client=object(), embedding_id='unit')
    kb.chunks = [TextChunk('page one', source, 1, 0, visual_pages=[1]),
                 TextChunk('page two', source, 2, 0, visual_pages=[2])]
    kb._vectors = [[1., 0.], [0., 1.]]
    kb.image_index = ImageVectorIndex('unit-image')
    kb.image_index.chunks = list(kb.chunks)
    kb.image_index.vectors = list(kb._vectors)
    view = scoped_index(kb, m['pages'])
    assert [c.page_number for c in view.chunks] == [1] and view._vectors == [[1., 0.]]
    assert [c.page_number for c in view.image_index.chunks] == [1] and view.image_index.vectors == [[1., 0.]]
    assert len(kb.chunks) == len(kb._vectors) == len(kb.image_index.chunks) == 2


def test_offline_prediction_ids_are_checked_against_actual_input_catalog(tmp_path):
    m = manifest(tmp_path)
    m['pages'][0]['elements'] = [{'id': '1', 'text': 'Intro', 'bbox': [0, 0, 1, 1]}]
    m['cases'][0].update(task='B', gold_element_ids=['1'])
    result = summarize(m['cases'], [{'id': 'q1', 'status': 'ok', 'element_ids': ['1', 'invented']}],
                       mode='oracle', pages=m['pages'])
    assert result['error_count'] == 1 and result['element_f1'] == 0.


def test_offline_rag_ranking_cannot_send_pages_outside_pdf_input_scope(tmp_path):
    m = manifest(tmp_path)
    m['pages'].append({**m['pages'][0], 'id': 'p2', 'page_number': 2})
    m['cases'][0].update(dataset='pdfvqa', task='A', answers=['yes'])
    result = summarize(m['cases'], [{'id': 'q1', 'status': 'ok', 'answer': 'yes', 'ranking': ['p2']}],
                       mode='rag', pages=m['pages'])
    assert result['error_count'] == 1 and result['accuracy'] == 0.
