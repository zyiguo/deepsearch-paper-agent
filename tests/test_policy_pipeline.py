from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from search_policy.dataset import (candidates, export_dataset, group_keys, read_jsonl,
                                   review_template, verify_dataset, write_new_jsonl)
from deepsearch_tracing.events import bind, emit
from search_policy.intent import SearchIntent
from deepsearch_tracing.store import TraceStore, validate_run
from deepsearch_agent.adapters.tracing import RAGTraceRecorder
from deepsearch_experiments.legacy.qa import AnswerService
from test_research_graph import Client, Library


class Model:
    def __init__(self, text='{"search":"灯塔"}'):
        self.text = text

    def complete_messages(self, messages):
        return self.text


def record(path, *, question='灯塔信号？', conversation=None, text='{"search":"灯塔"}',
           interrupted=False, execute=True):
    store = TraceStore(path)
    run_id = store.start({'question': question, 'conversation_id': conversation})
    with bind(lambda kind, data: store.append(run_id, kind, data)):
        decision = SearchIntent(Model(text), model_id='fixture', model_revision='v1').decide(question)
        if decision is not None and decision.action == 'search' and execute:
            emit('retrieval.request', {'search_id': 's1', 'query': decision.query,
                                      'origin': 'policy', 'policy_call_id': decision.call_id})
            emit('retrieval.result', {'search_id': 's1', 'chunks': [{'chunk_id': 'c1', 'text': 'fixture'}]})
    if not interrupted:
        store.finish(run_id)
    return run_id


def approve(rows):
    return [{**review, 'decision': 'approve', 'reviewer': 'test-human'} for review in review_template(rows)]


def test_exact_messages_raw_output_and_completed_run_roundtrip(tmp_path):
    path = tmp_path / 'traces.db'
    record(path, text=' {"search": "灯塔"} ')
    runs = TraceStore(path).read_runs()
    row = candidates(runs)[0]
    assert row['text'] == ' {"search": "灯塔"} '
    assert row['messages'][-1] == {'role': 'user', 'content': '灯塔信号？'}
    assert row['eligible'] and row['observations'][0]['chunks'][0]['chunk_id'] == 'c1'
    assert [e['seq'] for e in runs[0]['events']] == list(range(6))


@pytest.mark.parametrize('kwargs,reason', [
    ({'text': '{"answer":"UNKNOWN"}'}, 'answer_not_executed'),
    ({'text': 'bad'}, 'invalid_or_missing_action'),
    ({'interrupted': True}, 'run_not_completed'),
    ({'execute': False}, 'search_not_executed'),
])
def test_ineligible_traces_cannot_be_approved(tmp_path, kwargs, reason):
    record(tmp_path / 't.db', **kwargs)
    runs = TraceStore(tmp_path / 't.db').read_runs()
    row = candidates(runs)[0]
    assert row['reason'] == reason and not row['eligible']
    review = {'sample_id': row['sample_id'], 'sample_hash': row['sample_hash'],
              'decision': 'approve', 'reviewer': 'human'}
    with pytest.raises(ValueError, match='ineligible'):
        export_dataset(runs, [review], tmp_path / 'out')


def test_tampering_and_terminal_append_rejected(tmp_path):
    path = tmp_path / 't.db'
    run_id = record(path)
    store = TraceStore(path)
    with pytest.raises(ValueError, match='not open'):
        store.append(run_id, 'late', {})
    run = store.read_runs()[0]
    run['events'][1]['payload']['messages'][-1]['content'] = 'tampered'
    with pytest.raises(ValueError, match='hash'):
        validate_run(run)


def test_concurrent_runs_and_context_reset(tmp_path):
    path = tmp_path / 't.db'
    TraceStore(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: record(path, question=f'question-{i}'), range(12)))
    emit('outside', {'must': 'not be recorded'})
    runs = TraceStore(path).read_runs()
    assert len(runs) == 12
    for run in runs:
        assert len(run['events']) == 6
        assert candidates([run])[0]['messages'][-1]['content'] == run['metadata']['question']


def test_recording_failure_does_not_interrupt_work(tmp_path, monkeypatch):
    path = tmp_path / 't.db'
    monkeypatch.setattr(TraceStore, 'append', lambda *a: (_ for _ in ()).throw(OSError('private')))
    with RAGTraceRecorder(path).capture({'question': 'q'}) as observer:
        observer('event', {})
        emit('policy.event', {})
    assert TraceStore(path).read_runs()[0]['status'] == 'recording_error'


def test_start_failure_and_run_error(tmp_path, caplog):
    directory = tmp_path / 'directory'
    directory.mkdir()
    with RAGTraceRecorder(directory).capture({'question': 'q'}) as observer:
        assert observer is None
    path = tmp_path / 't.db'
    with pytest.raises(RuntimeError):
        with RAGTraceRecorder(path).capture({'question': 'q'}):
            raise RuntimeError('secret-provider-detail')
    run = TraceStore(path).read_runs()[0]
    assert run['status'] == 'error'
    assert 'secret-provider-detail' not in json.dumps(run) + caplog.text


def test_deterministic_reviewed_export_dedup_split_and_hashes(tmp_path):
    path = tmp_path / 't.db'
    for i in range(24):
        record(path, question=f'question-{i}', conversation=f'conversation-{i // 2}')
    record(path, question='question-0', conversation='conversation-0')
    runs = TraceStore(path).read_runs()
    reviews = approve(candidates(runs))
    a, b = tmp_path / 'a', tmp_path / 'b'
    manifest = export_dataset(runs, reviews, a)
    export_dataset(list(reversed(runs)), list(reversed(reviews)), b)
    for name in ('train.jsonl', 'val.jsonl', 'manifest.json'):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert manifest['counts']['deduplicated'] == 1
    assert verify_dataset(a) == manifest
    train, val = read_jsonl(a / 'train.jsonl'), read_jsonl(a / 'val.jsonl')
    assert train and val
    assert {r['group_id'] for r in train}.isdisjoint({r['group_id'] for r in val})
    for row in train + val:
        assert row['loss_scope'] == 'completion_only'
        assert len(row['messages']) == 2  # Observations are not appended to the input retroactively.
    with pytest.raises(FileExistsError):
        export_dataset(runs, reviews, a)
    (a / 'train.jsonl').write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='hash'):
        verify_dataset(a)


def test_question_and_conversation_transitive_grouping(tmp_path):
    path = tmp_path / 't.db'
    a = record(path, question='Q1', conversation='a')
    b = record(path, question='q1 ', conversation='b')
    c = record(path, question='Q2', conversation='b')
    groups = group_keys(TraceStore(path).read_runs())
    assert groups[a] == groups[b] == groups[c]


def test_stale_pending_duplicate_and_conflicting_labels(tmp_path):
    path = tmp_path / 't.db'
    record(path)
    runs = TraceStore(path).read_runs()
    rows = candidates(runs)
    with pytest.raises(ValueError, match='No approved'):
        export_dataset(runs, review_template(rows), tmp_path / 'pending')
    reviews = approve(rows)
    stale = deepcopy(reviews)
    stale[0]['sample_hash'] = 'changed'
    with pytest.raises(ValueError, match='stale'):
        export_dataset(runs, stale, tmp_path / 'stale')
    with pytest.raises(ValueError, match='duplicate'):
        export_dataset(runs, reviews * 2, tmp_path / 'duplicate')
    record(path, text='{"search":"different query"}')
    runs = TraceStore(path).read_runs()
    with pytest.raises(ValueError, match='Conflicting'):
        export_dataset(runs, approve(candidates(runs)), tmp_path / 'conflict')


def rag_client():
    return Client([{'claims': [{'text': '白光', 'citations': [1]}]},
                   {'supported': [0], 'queries': []}])


def test_rag_trace_records_real_policy_retrieval_and_verification(tmp_path):
    path = tmp_path / 't.db'
    policy = SearchIntent(Model(), model_id='fixture', model_revision='v1')
    unrecorded = AnswerService(rag_client(), Library(), use_graph=True, search_intent=policy).answer('信号？')
    recorded = AnswerService(rag_client(), Library(), use_graph=True, search_intent=policy,
        run_recorder=RAGTraceRecorder(path)).answer('信号？', trace_context={'conversation_id': 'session1'})
    assert recorded == unrecorded
    run = TraceStore(path).read_runs()[0]
    assert run['metadata']['conversation_id'] == 'session1'
    events = {e['kind']: e['payload'] for e in run['events']}
    assert events['retrieval.result']['chunks'][0]['page_number'] == 1
    assert events['rag.outcome']['status'] == 'verified'
    assert events['answer.final']['text'] == recorded.text
    assert candidates([run])[0]['eligible']


def test_rag_fallback_origin_is_not_attributed_to_policy(tmp_path):
    path = tmp_path / 't.db'
    main = Client([{'queries': ['fallback']}, {'claims': [{'text': '白光', 'citations': [1]}]},
                   {'supported': [0], 'queries': []}])
    AnswerService(main, Library(), use_graph=True, search_intent=SearchIntent(Model('bad')),
                  run_recorder=RAGTraceRecorder(path)).answer('信号？')
    run = TraceStore(path).read_runs()[0]
    event = next(e for e in run['events'] if e['kind'] == 'retrieval.request')
    assert event['payload']['origin'] == 'researcher'
    assert not candidates([run])[0]['eligible']


def test_retrieval_failure_keeps_attempt_but_not_training_sample(tmp_path):
    class BrokenLibrary:
        def search(self, *a, **kw):
            raise RuntimeError('private')

    path = tmp_path / 't.db'
    with pytest.raises(RuntimeError):
        AnswerService(rag_client(), BrokenLibrary(), use_graph=True, search_intent=SearchIntent(Model()),
                      run_recorder=RAGTraceRecorder(path)).answer('信号？')
    run = TraceStore(path).read_runs()[0]
    assert run['status'] == 'error'
    assert any(e['kind'] == 'retrieval.request' for e in run['events'])
    assert not candidates([run])[0]['eligible']


def test_core_import_without_site_packages_or_rag(tmp_path):
    src = str(Path(__file__).resolve().parents[1] / 'src')
    script = f'''import sys
sys.path.insert(0, {src!r})
import search_policy
import search_policy.dataset
import search_policy.__main__
assert not any(n.startswith(('deepsearch_agent', 'httpx', 'pydantic', 'langgraph')) for n in sys.modules)
print('standalone-ok')
'''
    result = subprocess.run([sys.executable, '-S', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'standalone-ok' in result.stdout


def test_web_session_records_conversation_without_credentials(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings

    outputs = [{'tool':'search_evidence','arguments':{'query':'模型怎么训练？'}}, {'tool':'','arguments':{}}, {'claims': [{'text': '对比学习', 'citations': [1]}]}, {'supported': [0], 'queries': []}]
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.build_llm', lambda _: Client(outputs))
    settings = LLMSettings(backend='openai_compatible', api_key='secret-do-not-record',
                           base_url='https://example.com/v1', model='main-fixture')
    path = tmp_path / 't.db'
    policy = SearchIntent(Model('{"search":"模型怎么训练？"}'), model_id='fixture', model_revision='v1')
    with TestClient(create_app(index_path=tmp_path / 'index.json', llm_settings=settings,
                              trace_path=path)) as client:
        client.post('/api/demo/ingest').raise_for_status()
        session_id = client.post('/api/sessions', json={'title': 'trace'}).json()['id']
        result = client.post(f'/api/sessions/{session_id}/turns',
                             json={'question': '模型怎么训练？', 'top_k': 1})
        assert result.status_code == 201
    runs = TraceStore(path).read_runs()
    assert runs[0]['metadata']['conversation_id'] == session_id
    assert runs[0]['metadata']['main_model'] == 'main-fixture'
    assert 'secret-do-not-record' not in json.dumps(runs)
    assert not candidates(runs)  # Production traces do not become policy training labels.



def test_production_rejects_legacy_search_policy(tmp_path):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    with pytest.raises(ValueError, match='offline'):
        with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings(),
                                  search_intent=SearchIntent(Model()))):
            pass

def test_scope_is_enforced_before_recording_evidence(tmp_path):
    from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
    from deepsearch_agent.core.evidence import TextChunk

    kb = KnowledgeBase(None, retrieval_mode='bm25')
    kb.chunks = [TextChunk('灯塔 白光', 'allowed.pdf', 1, 0),
                 TextChunk('灯塔 excluded-secret', 'excluded.pdf', 1, 0)]
    path = tmp_path / 't.db'
    AnswerService(rag_client(), kb, use_graph=True, search_intent=SearchIntent(Model()),
                  run_recorder=RAGTraceRecorder(path)).answer('灯塔信号？', allowed_sources={'allowed.pdf'})
    runs = TraceStore(path).read_runs()
    assert 'excluded-secret' not in json.dumps(runs)
    row = candidates(runs)[0]
    assert row['observations'][0]['chunks'][0]['source_path'] == 'allowed.pdf'


def test_finish_failure_leaves_non_exportable_running_trace(tmp_path, monkeypatch):
    path = tmp_path / 't.db'
    monkeypatch.setattr(TraceStore, 'finish', lambda *a: (_ for _ in ()).throw(OSError()))
    with RAGTraceRecorder(path).capture({'question': 'q'}):
        SearchIntent(Model()).decide('q')
    runs = TraceStore(path).read_runs()
    assert runs[0]['status'] == 'running'
    assert not candidates(runs)[0]['eligible']


def test_training_reader_passes_exact_decision_to_encoder(tmp_path):
    from search_policy.training import encode_sft_steps

    path = tmp_path / 't.db'
    record(path, text=' {"search": "灯塔"} ')
    runs = TraceStore(path).read_runs()
    export_dataset(runs, approve(candidates(runs)), tmp_path / 'dataset', val_ratio=0)
    calls = []

    def encoder(messages, text, tok, max_length):
        calls.append((messages, text, tok, max_length))
        return {'labels': [-100, 42]}

    items = list(encode_sft_steps(tmp_path / 'dataset', 'train', 'tokenizer-stub', 4096, encoder))
    assert len(items) == 1
    assert items[0]['labels'] == [-100, 42]
    assert calls[0][1:] == (' {"search": "灯塔"} ', 'tokenizer-stub', 4096)
    assert items[0]['question_id'] and items[0]['sample_id']


def test_cli_frozen_snapshot_to_verified_dataset(tmp_path):
    path = tmp_path / 't.db'
    record(path)
    runs = TraceStore(path).read_runs()
    raw, reviews = tmp_path / 'raw.jsonl', tmp_path / 'reviews.jsonl'
    write_new_jsonl(raw, runs)
    write_new_jsonl(reviews, approve(candidates(runs)))
    for arguments in (
        ['inspect', '--traces', str(raw)],
        ['export', '--traces', str(raw), '--reviews', str(reviews),
         '--output', str(tmp_path / 'dataset'), '--val-ratio', '0'],
        ['verify', str(tmp_path / 'dataset')],
    ):
        result = subprocess.run([sys.executable, '-m', 'search_policy', *arguments],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match='Duplicate trace'):
        candidates(runs * 2)
