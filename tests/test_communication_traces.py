from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys

import pytest

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_tracing.communication import handoffs, export_jsonl, export_split
from deepsearch_tracing.context import event_context, event_scope
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.agents.tool_service import PaperToolSession
from deepsearch_tracing.outcomes import classify_run
from deepsearch_agent.adapters.tracing import RAGTraceRecorder
from deepsearch_tracing.store import TraceStore, validate_run
from test_agentic_integration import Library, Planner


class FixtureClient(Planner):
    def __init__(self, library, *, rejected=False, crash=False, long=False, invalid_id=False):
        super().__init__()
        self.crash = crash
        text = ('a' * 850 + ' only under low noise') if long else 'verified local fact'
        self.outputs = iter([
            {'tool': 'search_evidence', 'arguments': {'query': 'fact'}},
            {'tool': '', 'arguments': {}, 'findings': [{'text': text,
                'evidence_ids': ['chk_' + 'f' * 64 if invalid_id else library.chunks[0].chunk_id]}]},
            {'claims': [{'text': 'verified local fact', 'citations': [1]}]},
            {'supported': [] if rejected else [0], 'queries': [],
             'issues': ['unsupported claim'] if rejected else []},
        ])

    def complete(self, prompt):
        if self.crash and prompt.startswith('你是 Researcher。'):
            raise RuntimeError('PRIVATE_EXCEPTION_DO_NOT_RECORD')
        return super().complete(prompt)


def record(path, **kwargs):
    library = Library()
    client = FixtureClient(library, **kwargs)

    class NoisySession(PaperToolSession):
        def search_evidence(self, *args, **kwargs):
            result = super().search_evidence(*args, **kwargs)
            result.content = json.dumps({'diagnostic': 'PRIVATE_CHILD_OBSERVATION ' + 'z' * 15000})
            return result

    service = AnswerService(client, library, use_graph=True,
        agent_settings=AgentSettings(enabled=True),
        tool_session_factory=lambda kb, **args: NoisySession(kb, **args),
        run_recorder=RAGTraceRecorder(path, metadata={'fixture': True}))
    answer = service.answer('fact?', history=[{'role': 'user', 'content': 'parent background'}],
                            trace_context={'conversation_id': 'fixture-session'})
    return answer, client


def test_complete_record_includes_both_boundaries_and_private_tools(tmp_path):
    path = tmp_path / 'traces.sqlite3'
    answer, client = record(path, long=True)
    run = TraceStore(path, read_only=True).read_runs()[0]
    row, = handoffs([run])
    assert run['metadata']['communication_trace_version'] == 1
    assert row['recording_status'] == 'completed'
    assert row['classification']['group'] == 'success'
    assert row['classification']['answer_correctness_label'] is None
    assert row['dispatch']['parent_context']['history'][0]['content'] == 'parent background'
    assert row['dispatch']['task']['goal'] == 'fact?'
    assert row['child_raw_results'] and row['evidence_snapshots']
    assert row['evidence_snapshots'][0]['payload']['chunks'][0]['text'] == 'verified local fact'
    observation = row['tool_observations'][0]['payload']
    assert len(observation['content']) > 15000
    assert len(json.dumps(observation['planner_observation'])) < 5000
    assert observation['trust'] == 'local'
    for event in row['source_events']:
        if event['kind'] in {'rag.tool_request', 'communication.tool_observation', 'tool.request'}:
            assert event['payload']['task_id'] == row['task_id']
    receipt = row['parent_receipts'][0]
    assert len(receipt['source_result']['findings'][0]['text']) > 500
    assert len(receipt['received_result']['findings'][0]['text']) <= 500
    received_data = receipt['model_inputs'][0]['payload']['data']
    assert received_data['subtask_results'][0] == receipt['received_result']
    assert receipt['model_requests'] and receipt['model_responses'] and receipt['decisions']
    assert row['parent_call_id'] == row['dispatch']['parent_call_id']
    assert all(row['stage_presence'].values())
    assert row['retrieval_config']['corpus_fingerprint']
    assert row['final_answer']['text'] == answer.text
    assert 'PRIVATE_CHILD_OBSERVATION' not in '\n'.join(p for p in client.prompts if p.startswith('你是 Supervisor。'))
    assert event_context() == {}


def test_normalized_result_keeps_raw_invalid_citation_for_attribution(tmp_path):
    record(tmp_path / 't.db', invalid_id=True)
    row, = handoffs(TraceStore(tmp_path / 't.db').read_runs())
    assert row['child_raw_results'][0]['findings'][0]['evidence_ids'] == ['chk_' + 'f' * 64]
    assert row['child_result']['findings'] == []
    assert 'invalid_evidence_reference' in row['child_result']['unresolved']


def test_failed_child_keeps_attempt_and_redacts_exception_detail(tmp_path):
    path = tmp_path / 't.db'
    with pytest.raises(RuntimeError, match='PRIVATE_EXCEPTION'):
        record(path, crash=True)
    run, = TraceStore(path).read_runs()
    row, = handoffs([run])
    assert row['classification']['group'] == 'failure'
    assert row['classification']['failure_stage'] == 'child_research'
    assert row['failures'][0]['payload']['error_type'] == 'RuntimeError'
    assert row['child_result'] is None and not row['parent_receipts']
    assert row['stage_presence']['result'] is False
    assert 'PRIVATE_EXCEPTION' not in json.dumps(run)


def test_success_failure_unknown_split_preserves_runs_without_handoff(tmp_path):
    path = tmp_path / 't.db'
    record(path)
    record(path, rejected=True)
    with RAGTraceRecorder(path).capture({'question': 'early error'}):
        pass  # Completed transport, no verifier: unknown rather than success.
    with pytest.raises(RuntimeError):
        with RAGTraceRecorder(path).capture({'question': 'before dispatch'}):
            raise RuntimeError('private')
    store = TraceStore(path)
    store.start({'question': 'interrupted'})
    runs = store.read_runs()
    groups = [classify_run(r)['group'] for r in runs]
    assert sorted(groups) == ['failure', 'failure', 'success', 'unknown', 'unknown']
    for run in runs:
        classified = [e['payload'] for e in run['events'] if e['kind'] == 'run.classified']
        if run['status'] != 'running':
            assert classified[-1] == classify_run(run)
    rows = handoffs(runs)
    manifest = export_split(rows, tmp_path / 'split', runs)
    assert manifest['files']['success/runs.jsonl']['rows'] == 1
    assert manifest['files']['failure/runs.jsonl']['rows'] == 2
    assert manifest['files']['failure/handoffs.jsonl']['rows'] == 1
    assert manifest['files']['unknown/runs.jsonl']['rows'] == 2
    for name, info in manifest['files'].items():
        assert hashlib.sha256((tmp_path / 'split' / name).read_bytes()).hexdigest() == info['sha256']
    with pytest.raises(FileExistsError):
        export_split(rows, tmp_path / 'split', runs)


def test_concurrent_tasks_do_not_mix_ids_or_outcomes(tmp_path):
    path = tmp_path / 't.db'
    TraceStore(path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda i: record(path, rejected=bool(i % 2)), range(4)))
    rows = handoffs(TraceStore(path).read_runs())
    assert len({r['task_id'] for r in rows}) == 4
    for row in rows:
        assert all(e['payload']['task_id'] == row['task_id'] for e in row['tool_observations'])
        assert row['child_result']['task_id'] == row['task_id']
    assert event_context() == {}


def test_recording_error_is_unknown_and_does_not_break_answer(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('secret disk detail')
    monkeypatch.setattr(TraceStore, 'append', fail)
    answer, _ = record(tmp_path / 't.db')
    assert answer.sources
    run, = TraceStore(tmp_path / 't.db').read_runs()
    assert classify_run(run)['group'] == 'unknown'
    assert classify_run(run)['outcome'] == 'recording_error'


def test_readonly_and_export_refuse_overwrite_or_tampering(tmp_path):
    path = tmp_path / 't.db'
    record(path)
    before = path.read_bytes()
    store = TraceStore(path, read_only=True)
    runs = store.read_runs()
    assert path.read_bytes() == before
    rows = handoffs(runs)
    export_jsonl(rows, tmp_path / 'rows.jsonl')
    with pytest.raises(FileExistsError):
        export_jsonl(rows, tmp_path / 'rows.jsonl')
    runs[0]['events'][0]['payload']['question'] = 'tampered'
    with pytest.raises(ValueError, match='hash'):
        handoffs(runs)


def test_cli_can_export_failure_group_without_model_calls(tmp_path):
    path = tmp_path / 't.db'
    record(path, rejected=True)
    result = subprocess.run([sys.executable, '-m', 'deepsearch_tracing.communication',
        'export', '--db', str(path), '--group', 'failure', '--output', str(tmp_path / 'badcases.jsonl')],
        capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report['handoffs'] == 1 and report['run_groups'] == {'failure': 1}


def test_scope_nested_restores_even_after_error():
    with event_scope(task_id='a'):
        with pytest.raises(RuntimeError), event_scope(task_id='b'):
            raise RuntimeError()
        assert event_context() == {'task_id': 'a'}
    assert event_context() == {}


def test_resume_keeps_receipt_without_inventing_child_execution(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    library = Library()
    path = tmp_path / 'traces.db'

    class Interrupt(FixtureClient):
        def complete(self, prompt):
            if prompt.startswith('你是 Supervisor。'):
                data = json.loads(prompt.split('\ndata: ', 1)[1])
                if data['research_rounds']:
                    raise RuntimeError('interrupt after persisted child result')
            return super().complete(prompt)

    with SqliteSaver.from_conn_string(str(tmp_path / 'checkpoint.db')) as saver:
        def service(client):
            return AnswerService(client, library, use_graph=True, checkpointer=saver,
                agent_settings=AgentSettings(enabled=True), run_recorder=RAGTraceRecorder(path))
        with pytest.raises(RuntimeError):
            service(Interrupt(library)).answer('fact?', run_id='business-run')
        client = FixtureClient(library)
        client.outputs = iter([{'claims': [{'text': 'verified local fact', 'citations': [1]}]},
                               {'supported': [0], 'queries': []}])
        assert service(client).answer('fact?', run_id='business-run', resume=True).sources
    runs = TraceStore(path).read_runs()
    original = next(r for r in runs if not r['metadata']['resume'])
    resumed = next(r for r in runs if r['metadata']['resume'])
    first, = handoffs([original])
    second, = handoffs([resumed])
    assert first['task_id'] == second['task_id']
    assert first['trace_run_id'] != second['trace_run_id']
    assert first['research_run_id'] == second['research_run_id'] == 'business-run'
    assert second['parent_receipts'] and second['dispatch'] is None
    assert second['child_result'] is None and not second['tool_observations']
    assert second['classification']['group'] == 'success'


def test_provider_diagnostics_and_cancellation_are_separate_from_quality(tmp_path):
    from deepsearch_agent.core.models import ModelServiceError
    from deepsearch_agent.core.cancellation import ResearchCancelled
    path = tmp_path / 't.db'
    for error in [ModelServiceError('secret provider body', error_code='rate_limit',
                                    provider_status_code=429), ResearchCancelled()]:
        with pytest.raises(type(error)):
            with RAGTraceRecorder(path).capture({'question': 'fixture'}):
                raise error
    runs = TraceStore(path).read_runs()
    labels = [classify_run(r) for r in runs]
    assert {c['outcome'] for c in labels} == {'model_service_error', 'cancelled'}
    provider = next(c for c in labels if c['outcome'] == 'model_service_error')
    assert provider['reason'] == 'rate_limit'
    assert provider['provider_diagnostics']['provider_status_code'] == 429
    assert provider['provider_diagnostics']['prompt_tokens'] is None
    assert 'secret provider body' not in json.dumps(runs)
