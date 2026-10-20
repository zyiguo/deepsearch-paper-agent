"""Architectural boundaries and pre-refactor data compatibility, without cloud calls."""
from pathlib import Path
import hashlib
import json
import pickle
import sqlite3
import subprocess
import sys
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'architecture_v1'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restore_sql(tmp_path, name):
    path = tmp_path / (name + '.sqlite3')
    with sqlite3.connect(path) as db:
        db.executescript((FIXTURES / (name + '.sql')).read_text(encoding='utf-8'))
    return path


def test_dependency_graph_has_no_cycles_or_forbidden_edges():
    from deepsearch_experiments.architecture import inspect
    result = inspect(ROOT / 'src')
    assert result['modules'] > 100
    assert result['cycles'] == []
    assert result['violations'] == []


def test_stdlib_contracts_traces_and_training_run_without_site_packages(tmp_path):
    script = f'''
import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.answers import Answer
from deepsearch_agent.core.models import LLMResponse, ModelServiceError
from deepsearch_agent.core.cancellation import CancellationToken
from deepsearch_agent.core.ports import RunRecorder
from deepsearch_tracing.recorder import RAGTraceRecorder
from deepsearch_tracing.store import TraceStore, validate_run
from deepsearch_tracing.outcomes import classify_run
import deepsearch_tracing.communication
import search_policy.dataset
import search_policy.training
import search_policy.__main__
from deepsearch_experiments.architecture import inspect
chunk = TextChunk('fixture', 'fixture.pdf', 1, 0)
chunk.validate_structure()
with RAGTraceRecorder({str(tmp_path / 'traces.sqlite3')!r}).capture({{'question':'fixture'}}) as emit:
    emit('rag.outcome', {{'status': 'verified'}})
    emit('answer.final', {{'text':'fixture', 'sources':[]}})
run = TraceStore({str(tmp_path / 'traces.sqlite3')!r}, read_only=True).read_runs()[0]
validate_run(run)
assert classify_run(run)['group'] == 'success'
assert not any(n.split('.')[0] in {{'fastapi','langgraph','httpx','pydantic','openai','redis'}} for n in sys.modules)
'''
    result = subprocess.run([sys.executable, '-S', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_production_startup_does_not_load_offline_or_trace_storage(tmp_path):
    script = f'''
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from deepsearch_agent.api import create_app
from deepsearch_agent.adapters.llm_config import LLMSettings
with TestClient(create_app(Path({str(tmp_path / 'index.json')!r}), llm_settings=LLMSettings())) as client:
    assert client.get('/api/health').status_code == 200
    assert client.post('/api/demo/ingest').status_code == 200
    assert client.post('/api/ask', json={{'question':'模型怎么训练？'}}).status_code == 200
    assert not any(n.startswith(('deepsearch_experiments', 'search_policy')) for n in sys.modules)
    assert 'deepsearch_tracing.store' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_pre_refactor_index_session_trace_and_checkpoint_read_without_rewriting(tmp_path):
    from deepsearch_agent.adapters.demo import build_demo_knowledge_base
    from deepsearch_agent.workspace.conversations import ConversationStore
    from deepsearch_agent.core.evidence import TextChunk
    from deepsearch_tracing.store import TraceStore, validate_run
    from deepsearch_tracing.outcomes import classify_run
    from deepsearch_agent.adapters.persistence.checkpoints import checkpoint_saver

    original = {p.name: digest(p) for p in FIXTURES.iterdir() if p.is_file()}
    kb = build_demo_knowledge_base()
    kb.load(FIXTURES / 'index.json')
    old = json.loads((FIXTURES / 'index.json').read_text(encoding='utf-8'))
    assert [c.chunk_id for c in kb.chunks] == [e['chunk']['chunk_id'] for e in old['entries']]
    for chunk in kb.chunks:
        chunk.validate_structure()
    sessions = ConversationStore(restore_sql(tmp_path, 'sessions'))
    session = sessions.list_sessions()[0]
    turn = sessions.get(session['id'])['turns'][0]
    assert turn['answer']['text'] == 'fixture answer [1]'
    traces = restore_sql(tmp_path, 'traces')
    before = digest(traces)
    run = TraceStore(traces, read_only=True).read_runs()[0]
    validate_run(run)
    assert not any(e['kind'] == 'run.classified' for e in run['events'])
    assert classify_run(run)['group'] == 'success'
    assert digest(traces) == before
    checkpoints = restore_sql(tmp_path, 'checkpoints')
    with sqlite3.connect(checkpoints) as db:
        checkpoint = checkpoint_saver(db).get_tuple({'configurable': {'thread_id': 'legacy-thread'}})
        evidence = checkpoint.checkpoint['channel_values']['evidence']
        assert all(isinstance(c, TextChunk) for c in evidence)
        assert [c.chunk_id for c in evidence] == [c.chunk_id for c in kb.chunks]
    assert {p.name: digest(p) for p in FIXTURES.iterdir() if p.is_file()} == original


def test_minimal_old_textchunk_pickle_alias():
    from deepsearch_agent.core.evidence import TextChunk
    from deepsearch_agent.chunking import TextChunk as LegacyChunk
    assert LegacyChunk is TextChunk
    chunk = TextChunk('fixture', 'fixture.pdf', 1, 0)
    original = TextChunk.__module__
    try:
        TextChunk.__module__ = 'deepsearch_agent.chunking'
        blob = pickle.dumps(chunk)
    finally:
        TextChunk.__module__ = original
    assert pickle.loads(blob) == chunk


def test_index_publish_failure_is_atomic_and_write_guard_is_enforced(tmp_path):
    from deepsearch_agent.knowledge.index_service import IndexService
    from deepsearch_agent.adapters.demo import build_demo_knowledge_base, DEMO_SOURCE
    from copy import deepcopy
    original = build_demo_knowledge_base()
    index = IndexService(original, tmp_path / 'index.json')
    index.save()
    before = digest(index.index_path)
    candidate = deepcopy(original)
    candidate.ingest_pdf(DEMO_SOURCE)
    candidate.save = lambda path: (_ for _ in ()).throw(OSError('fixture'))
    with pytest.raises(OSError):
        index.publish(candidate)
    assert index.kb is original
    assert digest(index.index_path) == before
    index.write_blocked = True
    with pytest.raises(ValueError):
        index.save()
    assert digest(index.index_path) == before


def test_owned_run_threads_cancel_and_finish_before_shutdown_returns():
    from deepsearch_agent.application.session_runs import RunWorkers
    from deepsearch_agent.core.cancellation import CancellationToken
    workers = RunWorkers()
    token = CancellationToken()
    started, finished = Event(), Event()
    def work():
        started.set()
        token.event.wait(timeout=5)
        finished.set()
    workers.start(work, token)
    assert started.wait(timeout=2)
    workers.close()
    assert token.event.is_set()
    assert finished.is_set()
    assert workers.active == {}
    with pytest.raises(RuntimeError, match='closed'):
        workers.start(work, CancellationToken())


@pytest.mark.parametrize('module', [
    'deepsearch_agent.communication_traces', 'deepsearch_agent.evaluation',
    'deepsearch_agent.mcp_runtime', 'deepsearch_agent.benchmarks',
    'deepsearch_agent.benchmarks.ablation', 'deepsearch_agent.benchmarks.ablation_report',
    'deepsearch_agent.benchmarks.paired_regression', 'deepsearch_agent.benchmarks.dag',
    'deepsearch_agent.benchmarks.multihop', 'search_policy',
])
def test_effective_cli_entrypoints_remain_available(module):
    result = subprocess.run([sys.executable, '-m', module, '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()


def test_pre_refactor_agent_checkpoint_resumes_in_current_mainline(tmp_path):
    from deepsearch_agent.adapters.persistence.checkpoints import checkpoint_saver
    from deepsearch_agent.adapters.demo import build_demo_knowledge_base
    from deepsearch_agent.agents.tool_service import PaperToolSession
    from deepsearch_agent.agents.runtime import ResearchWorkflow
    from test_research_graph import Client
    path = restore_sql(tmp_path, 'checkpoints')
    before = digest(FIXTURES / 'checkpoints.sql')
    kb = build_demo_knowledge_base()
    kb.load(FIXTURES / 'index.json')
    model = Client([{'claims':[{'text':'对比学习','citations':[1]}]},
                    {'supported':[0], 'queries':[]}])
    with sqlite3.connect(path, check_same_thread=False) as db:
        workflow = ResearchWorkflow(model, kb, tool_session=PaperToolSession(kb),
            checkpointer=checkpoint_saver(db), run_id='legacy-resume')
        result = workflow.answer('模型怎么训练？', 1, [], resume=True)
    assert result.text == '对比学习 [1]'
    assert result.sources[0].chunk_id == kb.chunks[1].chunk_id
    assert digest(FIXTURES / 'checkpoints.sql') == before


def test_recording_diagnostic_adapter_failure_preserves_business_exception(tmp_path):
    from deepsearch_tracing.recorder import RAGTraceRecorder
    from deepsearch_tracing.store import TraceStore
    from deepsearch_tracing.outcomes import classify_run
    def broken(error):
        raise RuntimeError('diagnostics fixture')
    recorder = RAGTraceRecorder(tmp_path / 'traces.sqlite3', error_adapter=broken)
    original = ValueError('business fixture')
    with pytest.raises(ValueError) as raised:
        with recorder.capture({'question':'fixture'}):
            raise original
    assert raised.value is original
    run = TraceStore(tmp_path / 'traces.sqlite3').read_runs()[0]
    assert classify_run(run)['group'] == 'failure'


def test_http_contract_matches_pre_refactor_snapshot():
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.mcp_config import MCPSettings
    app = create_app(llm_settings=LLMSettings(), mcp_settings=MCPSettings())
    current = app.openapi()
    previous = json.loads((FIXTURES / 'openapi.json').read_text(encoding='utf-8'))
    additions = {
        '/api/sessions/{session_id}/runs/{run_id}/operations': {'get'},
        '/api/sessions/{session_id}/runs/{run_id}/operations/{operation_id}/audit': {'get'},
        '/api/sessions/{session_id}/runs/{run_id}/operations/{operation_id}/reconcile': {'post'},
        '/api/documents/{ident}/consistency': {'get'},
        '/api/documents/{ident}/repair': {'post'},
        '/api/governance/usage': {'get'},
        '/api/governance/metrics': {'get'},
        '/api/governance/status': {'get'},
        '/api/governance/alerts': {'get'},
        '/api/governance/audit': {'get'},
        '/api/governance/limits/{scope}/{subject}': {'put'},
        '/api/governance/reservations/unknown': {'get'},
        '/api/governance/reservations/{ident}/settle': {'post'},
    }
    assert set(current['paths']) - set(previous['paths']) == set(additions)
    for path, methods in additions.items():
        assert set(current['paths'].pop(path)) == methods
    # Only the explicitly added, optional retry consent and inspection field
    # extend old contracts. Preserve the immutable original fixture.
    for action in ('retry', 'analyze', 'index-images'):
        params = current['paths'][f'/api/documents/{{ident}}/{action}']['post']['parameters']
        added = next(p for p in params if p['name'] == 'confirm_unknown')
        assert added['in'] == 'query' and not added['required'] and added['schema']['default'] is False
        params.remove(added)
    schemas = current['components']['schemas']
    schemas.pop('ReconcileOperationRequest')
    schemas.pop('QuotaLimit')
    schemas.pop('Settlement')
    assert schemas['DocumentResponse']['properties'].pop('retry_requires_confirmation')['default'] is False
    assert current == previous


def test_explicit_disabled_agent_rejects_live_startup_before_creating_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.agents.agent_config import AgentSettings
    calls = []
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.build_llm', lambda _: calls.append(True))
    settings = LLMSettings(backend='openai_compatible', api_key='fixture-key',
                           base_url='https://example.test/v1', model='fixture')
    config = tmp_path / '.env.agent'
    config.write_text('AGENTIC_RAG_ENABLED=false\n', encoding='utf-8')
    with pytest.raises(ValueError, match='offline'):
        with TestClient(create_app(tmp_path / 'index.json', llm_settings=settings,
                                  agent_settings=AgentSettings.load(config, environ={}))):
            pass
    assert calls == []


def test_partial_startup_failure_closes_resources_already_created(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.persistence import checkpoints
    from deepsearch_agent.workspace.documents import DocumentLibrary
    closed, connections = [], []
    class Client:
        def close(self):
            closed.append('model')
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.build_llm', lambda _: Client())
    original_saver, original_close = checkpoints.checkpoint_saver, DocumentLibrary.close
    def saver(connection):
        connections.append(connection)
        return original_saver(connection)
    def close_documents(self):
        closed.append('documents')
        original_close(self)
    def broken_skills(*args, **kwargs):
        raise RuntimeError('fixture startup failure')
    monkeypatch.setattr(checkpoints, 'checkpoint_saver', saver)
    monkeypatch.setattr(DocumentLibrary, 'close', close_documents)
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.SkillRegistry', broken_skills)
    with pytest.raises(RuntimeError, match='fixture startup failure'):
        with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())):
            pass
    assert closed == ['documents', 'model']
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')
