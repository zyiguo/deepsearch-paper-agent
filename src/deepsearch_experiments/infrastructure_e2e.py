"""Run opt-in real Docker/Redis acceptance against isolated synthetic resources."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4

PROBE = r'''
import json, os, time
from concurrent.futures import ThreadPoolExecutor
from deepsearch_agent.adapters.infrastructure import InfrastructureSettings, build_redis, CachedEmbeddingClient, RequestLimiter
class Provider:
    def __init__(self): self.calls = []
    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1., 0.] for _ in texts]
settings = InfrastructureSettings(redis_url=os.environ['REDIS_URL'], namespace=os.environ['INFRA_NAMESPACE']+'-probe', cache_ttl=2, rate_limit=5, rate_window=10)
backend = build_redis(settings)
try:
    assert backend.call('ping') is True
    provider = Provider()
    cache = CachedEmbeddingClient(provider, backend, 'model-a', 2, 'credential-a')
    assert cache.embed(['alpha', 'alpha']) == [[1.,0.], [1.,0.]]
    assert cache.embed(['alpha']) == [[1.,0.]]
    assert provider.calls == [['alpha']]
    ttl = backend.client.ttl(cache.key('alpha'))
    assert 0 < ttl <= 2
    assert 'alpha' not in cache.key('alpha') and 'credential-a' not in cache.key('alpha')
    isolated = []
    for model, scope in [('model-b','credential-a'), ('model-a','credential-b')]:
        other = CachedEmbeddingClient(Provider(), backend, model, 2, scope)
        other.embed(['alpha'])
        assert other.delegate.calls == [['alpha']]
        isolated.append(other.key('alpha'))
    assert len(set([cache.key('alpha'), *isolated])) == 3
    backend.client.set(cache.key('alpha'), '[0,0]', ex=2)
    assert cache.embed(['alpha']) == [[1.,0.]]
    assert len(provider.calls) == 2
    deadline = time.monotonic()+6
    while backend.client.exists(cache.key('alpha')) and time.monotonic()<deadline: time.sleep(.1)
    assert not backend.client.exists(cache.key('alpha'))
    cache.embed(['alpha'])
    assert len(provider.calls) == 3
    limiter = RequestLimiter(settings, backend)
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = sum(pool.map(lambda _: limiter.check('parallel-peer')[0], range(40)))
    assert accepted == 5
    print(json.dumps({'redis_ping': True, 'cache_hit': True, 'batch_deduplication': True, 'cache_ttl_seconds': ttl, 'ttl_expiration': True, 'corrupt_cache_refetched': True, 'model_and_credential_isolation': True, 'concurrent_requests':40, 'lua_accepted':accepted}))
finally:
    for key in backend.client.scan_iter(settings.namespace+':*'): backend.client.delete(key)
    backend.close()
'''


GRAPH_PROBE = r'''
import json, os
from deepsearch_agent.adapters.infrastructure import InfrastructureSettings, build_redis, CachedEmbeddingClient
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.pdf import FakePdfReader
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.adapters.tracing import RAGTraceRecorder
from deepsearch_tracing.store import TraceStore
from deepsearch_tracing.outcomes import classify_run
settings = InfrastructureSettings(redis_url=os.environ['REDIS_URL'], namespace=os.environ['INFRA_NAMESPACE']+'-graph')
backend = build_redis(settings)
class Provider:
    def __init__(self): self.calls=[]
    def embed(self,texts):
        self.calls.append(list(texts)); return [[1.,0.] for _ in texts]
class Planner:
    def __init__(self, chunk):
        self.outputs=iter([{'tool':'search_evidence','arguments':{'query':'reported value'}},
          {'tool':'','arguments':{},'findings':[{'text':'The value is 12.','evidence_ids':[chunk.chunk_id]}]},
          {'claims':[{'text':'The value is 12.','citations':[1]}]}, {'supported':[0],'queries':[]}])
    def complete(self,prompt):
        if prompt.startswith('你是 Supervisor。'):
            state=json.loads(prompt.split('\ndata: ',1)[1])
            action='research' if state['research_rounds']==0 else 'draft' if not state['verified'] else 'finish'
            return json.dumps({'action':action,'tasks':[],'instruction':'Find and verify the reported value'})
        return json.dumps(next(self.outputs))
try:
    provider=Provider()
    cached=CachedEmbeddingClient(provider,backend,'fixture-vector',2,'fixture-scope')
    kb=KnowledgeBase(FakePdfReader([]),embedding_client=cached,embedding_id='fixture-vector')
    chunk=TextChunk('The reported value is 12.','fixture.pdf',1,0)
    kb.ingest_chunks([chunk])
    path='/app/data/e2e-graph-traces.sqlite3'
    for _ in range(2):
        answer=AnswerService(Planner(chunk),kb,run_recorder=RAGTraceRecorder(path)).answer('What value is reported?')
        assert answer.text=='The value is 12. [1]' and answer.sources[0].chunk_id==chunk.chunk_id
    runs=TraceStore(path,read_only=True).read_runs()
    assert len(runs)==2 and all(classify_run(r)['group']=='success' for r in runs)
    assert all(any(e['kind']=='communication.task_dispatched' for e in r['events']) for r in runs)
    assert len(provider.calls)==2,provider.calls
    print(json.dumps({'production_graph_runs':2,'isolated_child_handoffs':2,'independently_verified':2,'success_traces':2,'embedding_provider_calls':2,'repeated_retrieval_cache_hit':True,'fixture_model':True}))
finally:
    for key in backend.client.scan_iter(settings.namespace+':*'): backend.client.delete(key)
    backend.close()
'''

OUTAGE_PROBE = r'''
import json, os, time
from deepsearch_agent.adapters.infrastructure import InfrastructureSettings, build_redis, CachedEmbeddingClient
settings=InfrastructureSettings(redis_url=os.environ['REDIS_URL'],namespace=os.environ['INFRA_NAMESPACE']+'-outage',socket_timeout=.2,cooldown=.2)
backend=build_redis(settings)
class Provider:
    def __init__(self):self.calls=0
    def embed(self,texts):self.calls+=1;return [[1.,0.] for _ in texts]
try:
    provider=Provider();cached=CachedEmbeddingClient(provider,backend,'fixture-vector',2)
    started=time.monotonic()
    assert cached.embed(['uncached'])==[[1.,0.]]
    assert cached.embed(['uncached'])==[[1.,0.]]
    elapsed=time.monotonic()-started
    assert provider.calls==2 and backend.status=='unavailable' and elapsed<2
    print(json.dumps({'real_outage_cache_bypassed':True,'embedding_calls':provider.calls,'elapsed_seconds':round(elapsed,3)}))
finally:backend.close()
'''


class Acceptance:
    def __init__(self, root, output, *, docker_host=None, docker_config=None, keep=False, build_proxy=None, environment=None):
        self.root, self.output, self.keep = Path(root).resolve(), Path(output).resolve(), keep
        self.output.mkdir(parents=True, exist_ok=False)
        self.project = 'deepsearch-e2e-' + uuid4().hex[:12]
        self.build_proxy = build_proxy
        self.environment = dict(environment or {})
        self.image = self.project + ':acceptance'
        self.cli = ['docker']
        if docker_host:
            self.cli += ['--host', docker_host]
        if docker_config:
            self.cli += ['--config', str(Path(docker_config).resolve())]
        self.file = self.output / 'compose.json'
        self.compose = ['compose', '--project-name', self.project, '--file', str(self.file)]
        self.steps = []
        self.base = None
        self.created = False
        self.built = False
        self.log_number = 0

    def run_command(self, args, *, input=None, timeout=120, env=None, docker=True, check=True):
        self.log_number += 1
        command = self.cli + args if docker else args
        result = subprocess.run(command, cwd=self.root, input=input, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=timeout, env=env)
        log = self.output / f'{self.log_number:02d}-command.log'
        log.write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        if check and result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}); see {log.name}')
        return result.stdout

    def compose_command(self, args, **kwargs):
        return self.run_command(self.compose + args, **kwargs)

    def record(self, name, **facts):
        self.steps.append({'name': name, 'status': 'passed', **facts})
        print(json.dumps(self.steps[-1], ensure_ascii=False), flush=True)

    def configuration(self):
        import yaml
        config = yaml.safe_load((self.root / 'compose.yaml').read_text(encoding='utf-8'))
        # Project names, ephemeral loopback ports and a fresh named volume isolate data.
        redis, api = config['services']['redis'], config['services']['api']
        redis['ports'] = ['127.0.0.1::6379']
        redis['restart'] = 'no'
        api.pop('profiles', None)
        api['build'] = {'context': str(self.root)}
        if self.build_proxy:
            api['build']['args'] = {'HTTP_PROXY': self.build_proxy, 'HTTPS_PROXY': self.build_proxy, 'NO_PROXY': 'localhost,127.0.0.1,redis'}
        api['image'] = self.image
        api['ports'] = ['127.0.0.1::8000']
        api['restart'] = 'no'
        api['environment'] = dict(REDIS_URL='redis://redis:6379/0', INFRA_NAMESPACE=self.project,
            API_RATE_LIMIT='5', API_RATE_WINDOW_SECONDS='10', REDIS_TIMEOUT_SECONDS='0.2',
            REDIS_COOLDOWN_SECONDS='0.2', LLM_BACKEND='demo', EMBEDDING_BACKEND='demo',
            VISION_BACKEND='disabled', IMAGE_EMBEDDING_BACKEND='disabled', MCP_ENABLED='false')
        api['environment'].update(self.environment)
        return config

    def setup(self):
        config = self.configuration()
        self.file.write_text(json.dumps(config, indent=2), encoding='utf-8')
        version = self.run_command(['version', '--format', '{{.Server.Version}}'], timeout=30).strip()
        self.compose_command(['config', '--quiet'])
        self.record('docker_engine_and_compose', server_version=version, project=self.project)
        print('Building the application image and pulling Redis...', flush=True)
        self.compose_command(['--progress', 'plain', 'build', '--pull=false', 'api'], timeout=900)
        self.built = True
        self.compose_command(['pull', '--policy', 'missing', 'redis'], timeout=300)
        image_id = self.run_command(['image', 'inspect', self.image, '--format', '{{.Id}}']).strip()
        self.record('application_image_built', image_id=image_id)
        self.created = True
        self.compose_command(['up', '-d'], timeout=180)
        self.refresh_endpoint()
        self.wait_ready('ok')
        uid = self.compose_command(['exec', '-T', 'api', 'id', '-u']).strip()
        assert uid == '10001', uid
        self.record('container_startup', non_root_uid=uid, endpoint=self.base)

    def refresh_endpoint(self):
        # Docker may reassign an ephemeral published port when a container restarts.
        port = self.compose_command(['port', 'api', '8000']).strip().rsplit(':', 1)[1]
        self.base = f'http://127.0.0.1:{port}'

    def request(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = Request(self.base + path, data=data, method=method,
            headers={'Content-Type': 'application/json', 'X-Request-ID': self.project})
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(req, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            body = json.loads(response.read())
            return response.status, {key.lower(): value for key, value in response.headers.items()}, body

    def wait_ready(self, expected, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.request('GET', '/api/health/ready')
                if last[0] == 200 and last[2]['status'] == expected:
                    return last[2]
            except (URLError, OSError, TimeoutError, ValueError):
                pass
            time.sleep(.5)
        raise RuntimeError(f'API readiness did not become {expected}; last={last}')

    def reset_rate_window(self):
        keys = self.compose_command(['exec', '-T', 'redis', 'redis-cli', '--raw', '--scan',
                                     '--pattern', self.project + ':rate:*']).splitlines()
        # Delete only this new test project's rate keys, never a database-wide flush.
        for key in filter(None, keys):
            assert key.startswith(self.project + ':rate:')
            self.compose_command(['exec', '-T', 'redis', 'redis-cli', 'DEL', key])

    def verify(self):
        status, _, body = self.request('POST', '/api/demo/ingest')
        assert status == 200 and body['added_chunks'] == 2
        status, headers, session = self.request('POST', '/api/sessions', {'title':'container acceptance'})
        assert status == 201 and headers.get('x-request-id') == self.project
        session_id = session['id']
        status, _, turn = self.request('POST', f'/api/sessions/{session_id}/turns', {'question':'模型怎么训练？'})
        assert status == 201 and turn['answer']['sources']
        self.record('http_ingestion_and_persisted_answer', session_created=True, evidence_count=len(turn['answer']['sources']), request_id=True)
        graph = self.compose_command(['exec', '-T', 'api', 'python', '-'], input=GRAPH_PROBE)
        self.record('container_production_agent_graph', **json.loads(graph.strip().splitlines()[-1]))
        container_id = self.compose_command(['ps', '-q', 'api']).strip()
        self.run_command(['cp', container_id+':/app/data/e2e-graph-traces.sqlite3', str(self.output/'fixture-graph-traces.sqlite3')])
        probe = self.compose_command(['exec', '-T', 'api', 'python', '-'], input=PROBE)
        self.record('real_redis_cache_and_lua', **json.loads(probe.strip().splitlines()[-1]))
        redis_port = self.compose_command(['port', 'redis', '6379']).strip().rsplit(':', 1)[1]
        env = {**os.environ, 'TEST_REDIS_URL':f'redis://127.0.0.1:{redis_port}/15'}
        result = self.run_command([sys.executable, '-m', 'pytest', 'tests', '-q',
                                  '--basetemp', str(self.output / 'pytest-temp')],
                                  env=env, docker=False, timeout=180)
        assert 'failed' not in result.lower(), result
        self.record('host_full_regression_with_real_redis', pytest_summary=result.strip().splitlines()[-1])
        self.reset_rate_window()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request('POST', '/api/sessions', {'title':'parallel acceptance'}), range(20)))
        assert sum(r[0] == 201 for r in results) == 5
        assert sum(r[0] == 429 for r in results) == 15
        assert all(r[1].get('retry-after') and r[1].get('x-request-id') for r in results if r[0] == 429)
        self.record('http_atomic_limit', attempted=20, accepted=5, rejected_429=15, retry_after=True)
        self.compose_command(['stop', 'redis'])
        ready = self.wait_ready('degraded')
        assert ready['dependencies']['rate_limit'] == 'local'
        cache_fallback = self.compose_command(['exec', '-T', 'api', 'python', '-'], input=OUTAGE_PROBE)
        self.record('real_redis_cache_outage_fallback', **json.loads(cache_fallback.strip().splitlines()[-1]))
        results = [self.request('POST', '/api/ask', {'question':'模型怎么训练？'})]
        results += [self.request('POST', '/api/sessions', {'title':'outage acceptance'}) for _ in range(5)]
        assert [r[0] for r in results] == [200, 201, 201, 201, 201, 429]
        self.record('redis_outage_http_fallback', ready=ready, answer_available=True, local_limit_enforced=True)
        self.compose_command(['start', 'redis'])
        ready = self.wait_ready('ok')
        assert ready['dependencies']['rate_limit'] == 'redis'
        assert self.request('POST','/api/sessions',{'title':'recovery acceptance'})[0] == 201
        self.record('redis_recovery', ready=ready, writes_available=True)
        self.compose_command(['restart', 'api'])
        self.refresh_endpoint()
        self.wait_ready('ok')
        status, _, saved = self.request('GET', f'/api/sessions/{session_id}')
        assert status == 200 and saved['turns'][0]['answer'] == turn['answer']
        status, _, persistence = self.request('GET', '/api/knowledge-base/status')
        assert status == 200 and persistence['snapshot_exists'] and persistence['chunk_count'] == 2
        self.record('api_restart_preserves_volume', saved_turn=True, index_chunks=2)

    def cleanup(self):
        if not self.created:
            if self.built and not self.keep:
                self.run_command(['image', 'rm', self.image], check=False)
            return
        self.compose_command(['logs', '--no-color'], check=False)
        if self.keep:
            return
        volumes = self.run_command(['volume', 'ls', '--filter', 'label=com.docker.compose.project='+self.project, '-q']).splitlines()
        for volume in filter(None, volumes):
            details = json.loads(self.run_command(['volume','inspect',volume]))[0]
            assert details['Labels']['com.docker.compose.project'] == self.project
        self.compose_command(['down', '--remove-orphans'])
        for volume in filter(None, volumes):
            self.run_command(['volume', 'rm', volume])
        self.run_command(['image','rm',self.image],check=False)
        self.record('isolated_test_resources_cleaned', volumes_removed=len(volumes))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='A new output directory; never overwrite an existing report')
    parser.add_argument('--docker-host')
    parser.add_argument('--docker-config', type=Path)
    parser.add_argument('--build-proxy', help='Optional build-only HTTP(S) proxy; does not modify Docker Desktop settings')
    parser.add_argument('--keep', action='store_true', help='Keep only the isolated test containers and synthetic data')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    run = Acceptance(root, args.output, docker_host=args.docker_host, docker_config=args.docker_config, keep=args.keep, build_proxy=args.build_proxy)
    error = None
    started = datetime.now(timezone.utc).isoformat()
    try:
        run.setup()
        run.verify()
    except Exception as exc:
        error = {'type':type(exc).__name__, 'message':str(exc),
                 'location': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                              for f in traceback.extract_tb(exc.__traceback__)]}
        print(json.dumps({'status':'failed', **error}), flush=True)
    finally:
        try:
            run.cleanup()
        except Exception as exc:
            error = error or {'type':type(exc).__name__, 'message':str(exc)}
        report = {'schema_version':1, 'started_at':started, 'finished_at':datetime.now(timezone.utc).isoformat(),
            'status':'passed' if error is None else 'failed', 'project':run.project,
            'synthetic_business_inputs':True, 'cloud_model_calls':False, 'kept':args.keep,
            'steps':run.steps, 'error':error,
            'source_sha256':hashlib.sha256((root/'Dockerfile').read_bytes()+(root/'compose.yaml').read_bytes()).hexdigest()}
        (run.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'report':str(run.output/'report.json')},ensure_ascii=False),flush=True)
    return int(error is not None)


if __name__ == '__main__':
    raise SystemExit(main())
