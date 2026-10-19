import json

import pytest
from pydantic import ValidationError

from deepsearch_experiments.benchmarks.multihop.datasets import import_ircot, import_logicrag, load_manifest, write_manifest
from deepsearch_experiments.benchmarks.multihop.metrics import answer_scores, evaluate_predictions, normalize_answer
from deepsearch_experiments.benchmarks.multihop.schema import AgentCase, Prediction, document_id, manifest_sha256


REVISION = 'a' * 40


def test_logicrag_2wiki_keeps_source_warnings_and_identical_duplicates(tmp_path):
    questions, corpus = tmp_path / 'questions.json', tmp_path / 'corpus.json'
    questions.write_text(json.dumps([{'_id': 'q', 'question': 'Where?', 'answer': 'Paris',
        'context': [['Person', ['Born in Paris.']], ['Person', ['Born in Paris.']]],
        'supporting_facts': [['Person', 3]], 'evidences': [['PRIVATE', 'PRIVATE', 'PRIVATE']]}]))
    corpus.write_text(json.dumps([{'title': 'Person', 'text': 'Born in Paris.'}]))
    manifest = import_logicrag(questions, corpus, dataset='2wikimultihopqa', source_revision=REVISION)
    assert manifest.protocol == 'logicrag-released-v1'
    assert manifest.corpus_scope == 'sampled_pool'
    assert len(manifest.annotation_warnings) == 1
    assert 'PRIVATE' not in manifest.cases[0].model_dump_json()
    assert len(manifest.gold[0].support_document_ids) == 1


def test_logicrag_musique_aliases_and_unanswerable_rejection(tmp_path):
    questions, corpus = tmp_path / 'questions.json', tmp_path / 'corpus.json'
    row = {'id': 'q', 'question': 'Where?', 'answer': 'Paris', 'answer_aliases': ['City of Paris'],
           'answerable': True, 'paragraphs': [{'idx': 0, 'title': 'Person',
               'paragraph_text': 'Born in Paris.', 'is_supporting': True}]}
    questions.write_text(json.dumps([row]))
    corpus.write_text(json.dumps([{'title': 'Person', 'text': 'Born in Paris.'}]))
    manifest = import_logicrag(questions, corpus, dataset='musique', source_revision=REVISION)
    assert set(manifest.gold[0].answers) == {'Paris', 'City of Paris'}
    row['answerable'] = False
    questions.write_text(json.dumps([row]))
    with pytest.raises(ValueError, match='MuSiQue-Ans'):
        import_logicrag(questions, corpus, dataset='musique', source_revision=REVISION)


def source_row(ident='q1', answer='Paris'):
    return {
        'question_id': ident, 'question_text': 'Where is the birthplace?',
        'answers_objects': [{'number': '', 'date': {'day': '', 'month': '', 'year': ''}, 'spans': [answer]}],
        'contexts': [
            {'idx': 0, 'title': 'Person', 'paragraph_text': 'The person was born in Paris.', 'is_supporting': True},
            {'idx': 1, 'title': 'Distractor', 'paragraph_text': 'A different place.', 'is_supporting': False},
        ],
        'reasoning_steps': ['Private gold reasoning >>>> Paris'],
    }


def write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def imported(tmp_path, rows=None, dataset='musique', **kwargs):
    questions, corpus = tmp_path / 'questions.jsonl', tmp_path / 'corpus.jsonl'
    write_jsonl(questions, rows or [source_row()])
    write_jsonl(corpus, [
        {'title': 'Person', 'text': 'The person was born in Paris.'},
        {'title': 'Distractor', 'paragraph_text': 'A different place.'},
    ])
    return import_ircot(questions, corpus, dataset=dataset, split='dev_subsampled',
                        corpus_scope='sampled_pool', source_revision=REVISION, **kwargs)


def test_import_hides_labels_from_agent_and_preserves_identity(tmp_path):
    manifest = imported(tmp_path)
    assert manifest.cases[0].model_dump() == {'id': 'q1', 'question': 'Where is the birthplace?'}
    assert 'Paris' not in manifest.cases[0].model_dump_json()
    assert manifest.gold[0].answers == ['Paris']
    assert manifest.gold[0].support_document_ids == [document_id('Person', 'The person was born in Paris.')]
    assert {source.kind for source in manifest.sources} == {'questions', 'corpus'}
    assert all(len(source.sha256) == 64 for source in manifest.sources)


def test_strict_agent_and_prediction_contracts():
    with pytest.raises(ValidationError):
        AgentCase(id='q', question='Question', answers=['gold'])
    with pytest.raises(ValidationError):
        AgentCase(id=1, question='Question')
    with pytest.raises(ValidationError):
        Prediction(answer=42)
    with pytest.raises(ValidationError):
        Prediction(retrieved_document_ids=['d', 'd'])


def test_sampling_is_reproducible_and_order_independent(tmp_path):
    rows = [source_row(f'q{index}') for index in range(20)]
    first = imported(tmp_path, rows, seed=19, limit=5)
    second = imported(tmp_path, list(reversed(rows)), seed=19, limit=5)
    assert [case.id for case in first.cases] == [case.id for case in second.cases]
    assert [gold.case_sha256 for gold in first.gold] == [gold.case_sha256 for gold in second.gold]
    assert first.sources[0].sha256 != second.sources[0].sha256
    assert first.available_cases == 20


@pytest.mark.parametrize('rows,message', [
    ([source_row(), source_row()], 'Duplicate question'),
    ([source_row(answer='')], 'Unlabelled'),
])
def test_import_rejects_bad_questions(tmp_path, rows, message):
    with pytest.raises(ValueError, match=message):
        imported(tmp_path, rows)


def test_support_must_be_in_external_corpus(tmp_path):
    row = source_row()
    row['contexts'][0]['paragraph_text'] = 'A different gold paragraph.'
    with pytest.raises(ValueError, match='Gold documents missing'):
        imported(tmp_path, [row])


def test_rejects_missing_support_annotations(tmp_path):
    row = source_row()
    row['contexts'][0]['is_supporting'] = False
    with pytest.raises(ValueError, match='No gold supporting'):
        imported(tmp_path, [row])


def test_roundtrip_fingerprint_and_tamper_detection(tmp_path):
    manifest = imported(tmp_path)
    path = tmp_path / 'manifest.json'
    write_manifest(manifest, path)
    assert manifest_sha256(load_manifest(path)) == manifest_sha256(manifest)
    with pytest.raises(FileExistsError):
        write_manifest(manifest, path)
    payload = json.loads(path.read_text())
    payload['gold'][0]['answers'] = ['London']
    path.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match='fingerprint mismatch'):
        load_manifest(path)


def test_full_corpus_needs_author_count_and_revision_is_pinned(tmp_path):
    manifest = imported(tmp_path)
    payload = manifest.model_dump()
    payload['corpus_scope'] = 'full'
    with pytest.raises(ValidationError, match='author-reported'):
        type(manifest).model_validate(payload)
    payload = manifest.model_dump()
    payload['source_revision'] = 'main'
    with pytest.raises(ValidationError):
        type(manifest).model_validate(payload)


def test_aliases_are_scored_only_when_explicitly_supplied(tmp_path):
    aliases = tmp_path / 'aliases.json'
    aliases.write_text(json.dumps({'q1': ['City of Paris']}))
    manifest = imported(tmp_path, aliases_path=aliases)
    report = evaluate_predictions(manifest, {'q1': {'answer': 'City of Paris'}})
    assert report['summary']['answer_em'] == 1
    assert report['alias_policy'] == 'supplied-question-aliases'
    aliases.write_text(json.dumps({'unknown': ['wrong']}))
    with pytest.raises(ValueError, match='unknown question'):
        imported(tmp_path, aliases_path=aliases)


def test_missing_and_failed_predictions_stay_in_denominator(tmp_path):
    manifest = imported(tmp_path, [source_row('q1'), source_row('q2'), source_row('q3')])
    supporting = manifest.gold[0].support_document_ids
    report = evaluate_predictions(manifest, {
        'q1': {'answer': 'Paris', 'retrieved_document_ids': supporting},
        'q2': {'answer': 'Paris', 'status': 'error', 'retrieved_document_ids': supporting},
    })
    assert report['summary']['count'] == 3
    assert report['summary']['answer_em'] == pytest.approx(1 / 3)
    assert report['summary']['document_support_recall_proxy'] == pytest.approx(2 / 3)
    assert report['summary']['missing_predictions'] == 1
    assert report['summary']['failed_predictions'] == 1
    assert 'official_sentence_support' in report['not_implemented']


def test_unknown_prediction_ids_rejected(tmp_path):
    manifest = imported(tmp_path)
    with pytest.raises(ValueError, match='unknown question'):
        evaluate_predictions(manifest, {'unknown': {'answer': 'Paris'}})
    with pytest.raises(ValueError, match='unknown document'):
        evaluate_predictions(manifest, {'q1': {'answer': 'Paris', 'retrieved_document_ids': ['unknown']}})


def test_answer_normalization_and_dataset_specific_boolean_f1():
    assert normalize_answer(' The Paris, France! ') == 'paris france'
    assert answer_scores('Paris', ['The Paris', 'Paris city'], 'musique')['answer_em'] == 1
    assert answer_scores('Paris', ['Paris city'], 'musique')['answer_f1'] == pytest.approx(2 / 3)
    assert answer_scores('yes indeed', ['yes'], '2wikimultihopqa')['answer_f1'] == 0
    assert answer_scores('yes indeed', ['yes'], 'musique')['answer_f1'] == pytest.approx(2 / 3)
    assert answer_scores('', ['The'], 'musique') == {'answer_em': 0, 'answer_f1': 0}


def test_duplicate_corpus_ids_and_content_rejected(tmp_path):
    imported(tmp_path)
    corpus = tmp_path / 'corpus.jsonl'
    row = {'id': 'sourceid', 'title': 'Person', 'text': 'The person was born in Paris.'}
    write_jsonl(corpus, [row, row])
    with pytest.raises(ValueError, match='duplicate upstream corpus'):
        import_ircot(tmp_path / 'questions.jsonl', corpus, dataset='musique', split='dev',
                     corpus_scope='external', source_revision=REVISION)
    row.pop('id')
    write_jsonl(corpus, [row, row])
    with pytest.raises(ValueError, match='Duplicate document content'):
        import_ircot(tmp_path / 'questions.jsonl', corpus, dataset='musique', split='dev',
                     corpus_scope='external', source_revision=REVISION)
