"""Production ASGI/agent graph load baseline with local synthetic providers only."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ctypes
import hashlib
import json
import os
import math
import gc
from pathlib import Path
from threading import Event, Thread
import time
import tracemalloc
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.application.bootstrap import create_app
from deepsearch_agent.core.evidence import TextChunk


def rss_bytes():
    if os.name=='nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
                ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
                ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
        kernel=ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi=ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        row=Counters(); row.cb=ctypes.sizeof(row)
        return int(row.WorkingSetSize) if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(row),row.cb) else None
    path=Path('/proc/self/statm')
    return int(path.read_text().split()[1])*os.sysconf('SC_PAGE_SIZE') if path.is_file() else None


def provider_response(request, delay):
    time.sleep(delay)
    prompt=json.loads(request.content)['messages'][0]['content']
    if prompt.startswith('你是一个检索结果重排序器。'):
        result={'ranked_ids':[1]}
    else:
        data=json.loads(prompt.split('\ndata: ',1)[1])
        if prompt.startswith('你是 Supervisor。'):
            result={'action':'research' if not data['research_rounds'] else 'finish' if data['verified'] else 'draft',
                    'tasks':[],'instruction':'验证本地训练方法'}
        elif prompt.startswith('你是 Researcher。'):
            result={'tool':'' if data['observations'] else 'search_evidence',
                    'arguments':{} if data['observations'] else {'query':'模型怎么训练？','top_k':1}}
        elif prompt.startswith('你是 Answerer。'):
            result={'claims':[{'text':'模型使用对比学习训练。','citations':[1]}]}
        elif prompt.startswith('你是 Verifier。'):
            result={'supported':[0],'queries':[]}
        else:
            raise AssertionError('Unexpected synthetic provider prompt')
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)},'finish_reason':'stop'}],
                                   'usage':{'total_tokens':32}})


def percentile(values, fraction):
    ordered=sorted(values)
    return ordered[max(0,min(len(ordered)-1,math.ceil(len(ordered)*fraction)-1))] if ordered else None


def trial(root, documents, concurrency, requests, delay, dimensions):
    gc.collect()
    root.mkdir()
    settings=LLMSettings(backend='openai_compatible',api_key='synthetic-not-a-real-key',base_url='https://example.test/v1',
                         model='synthetic-capacity',max_output_tokens=512,context_window=65536)
    limits=GovernanceSettings(global_research=4,workspace_research=4,user_research=4,
                              global_provider=2,workspace_provider=2,user_provider=2)
    access=AccessSettings(mode='team',root=root/'private-state',cookie_secure=False,origins=('http://testserver',),max_open_workspaces=3)
    app=create_app(llm_settings=settings,access_settings=access,governance_settings=limits)
    factory=lambda config:OpenAICompatibleClient(config,transport=httpx.MockTransport(lambda request:provider_response(request,delay)))
    write={'X-DeepSearch-Request':'1'}
    with patch('deepsearch_agent.application.bootstrap.build_llm',factory),TestClient(app) as client:
        app.state.access.create_user('capacity-user','Capacity user','synthetic-capacity-password-20261020')
        assert client.post('/api/access/login',json={'username':'capacity-user','password':'synthetic-capacity-password-20261020'},headers=write).status_code==200
        workspaces=[]
        for number in range(3):
            wid=client.post('/api/workspaces',json={'name':f'Capacity {number}'},headers=write).json()['id']
            workspaces.append(wid)
            client.get(f'/w/{wid}/api/health').raise_for_status()
            runtime=app.state.workspace_runtimes.apps[wid]
            candidate=deepcopy(runtime.state.kb)
            class SyntheticEmbeddings:
                def __deepcopy__(self,memo):
                    return self
                def embed(self,texts):
                    return [[1.]+[0.]*(dimensions-1) for _ in texts]
            candidate.embedding_client=SyntheticEmbeddings()
            candidate.embedding_id='synthetic-capacity-'+str(dimensions)
            candidate.retrieval_mode='hybrid'
            candidate.ingest_chunks([TextChunk('模型使用对比学习训练。',
                'pdf://'+hashlib.sha256(f'capacity-{i}'.encode()).hexdigest()+'/fixture.pdf',1,0) for i in range(documents)])
            runtime.state.index.publish(candidate)
        targets=[]
        for i in range(requests):
            prefix=f'/w/{workspaces[i%3]}/api'
            sid=client.post(prefix+'/sessions',json={'title':f'Capacity {i}'},headers=write).json()['id']
            targets.append(prefix+f'/sessions/{sid}/turns')
        # Warmup is separate from measured requests.
        prefix=f'/w/{workspaces[0]}/api'
        warmup_sid=client.post(prefix+'/sessions',json={'title':'Separate warmup'},headers=write).json()['id']
        warmup=client.post(prefix+f'/sessions/{warmup_sid}/turns',json={'question':'模型怎么训练？','top_k':1},headers=write)
        assert warmup.status_code==201,warmup.text
        tracemalloc.start()
        baseline_rss=rss_bytes()
        samples=[]
        stop=Event()
        def sample_memory():
            while not stop.wait(.01):
                value=rss_bytes()
                if value is not None:
                    samples.append(value)
        sampler=Thread(target=sample_memory,daemon=True); sampler.start()
        def send(target):
            started=time.perf_counter()
            response=client.post(target,json={'question':'模型怎么训练？','top_k':1},headers=write)
            return {'seconds':time.perf_counter()-started,'http_status':response.status_code,
                    'completed':response.status_code==201,
                    'has_citations':bool(response.json().get('answer',{}).get('sources')),
                    'code':response.json().get('detail') if response.status_code!=201 else None}
        started=time.perf_counter()
        try:
            with ThreadPoolExecutor(concurrency) as pool:
                raw=list(pool.map(send,targets))
        finally:
            stop.set(); sampler.join(timeout=2)
        elapsed=time.perf_counter()-started
        _,python_peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
        snapshot=app.state.governance.snapshot()
        completed=[r for r in raw if r['completed']]
        rejected=[r for r in raw if r['http_status'] in {429,503}]
        assert len(completed)+len(rejected)==requests,raw
        assert snapshot['resources']['provider']['peak']<=2
        assert snapshot['resources']['research']['peak']<=4
        assert all(row['active']==0 for row in snapshot['resources'].values())
        assert snapshot['expenses']['unknown_count']==0,snapshot
        return {'documents_per_workspace':documents,'chunks_per_document':1,'embedding_dimensions':dimensions,
                'workspaces':3,'concurrency':concurrency,'requests':requests,
                'completed':len(completed),'with_citations':sum(r['has_citations'] for r in completed),'rejected':len(rejected),
                'http_statuses':dict(Counter(str(r['http_status']) for r in raw)),
                'elapsed_seconds':elapsed,'completed_per_second':len(completed)/elapsed,
                'all_latency_p50_seconds':percentile([r['seconds'] for r in raw],.5),
                'all_latency_p95_seconds':percentile([r['seconds'] for r in raw],.95),
                'completed_latency_p95_seconds':percentile([r['seconds'] for r in completed],.95),
                'rss_baseline_bytes':baseline_rss,'rss_peak_sampled_bytes':max(samples) if samples else None,
                'python_heap_peak_bytes':python_peak,'resources':snapshot['resources'],'raw':raw}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--documents',default='100,1000')
    parser.add_argument('--concurrency',default='1,4,12')
    parser.add_argument('--requests',type=int,default=12)
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--provider-delay',type=float,default=.01)
    parser.add_argument('--dimensions',type=int,default=1024)
    args=parser.parse_args()
    sizes=[int(v) for v in args.documents.split(',')]
    levels=[int(v) for v in args.concurrency.split(',')]
    if (not 1<=args.requests<=1000 or not 1<=args.repeats<=10 or not 0<=args.provider_delay<=1 or not 2<=args.dimensions<=4096
            or any(not 1<=n<=10000 for n in sizes) or any(not 1<=n<=64 for n in levels)):
        parser.error('Invalid bounded baseline settings')
    args.output.mkdir(parents=True,exist_ok=False)
    source_paths=[*Path('src/deepsearch_agent').rglob('*.py'),Path(__file__)]
    repository=Path(__file__).resolve().parents[1]
    source_hashes={p.resolve().relative_to(repository).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    with (args.output/'source-manifest.json').open('x',encoding='utf-8') as stream:
        json.dump(source_hashes,stream,indent=2)
    results=[]
    for size in sizes:
        for concurrency in levels:
            for repeat in range(args.repeats):
                row=trial(args.output/f'case-{len(results):02d}',size,concurrency,args.requests,args.provider_delay,args.dimensions)
                row['repeat']=repeat+1
                results.append(row)
                with (args.output/f'trial-{len(results):02d}.json').open('x',encoding='utf-8') as stream:
                    json.dump(row,stream,ensure_ascii=False,indent=2)
                print(json.dumps({k:row[k] for k in ('documents_per_workspace','concurrency','repeat','completed','rejected','elapsed_seconds')},ensure_ascii=False),flush=True)
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in source_hashes.items()),'Source changed during baseline; preserve partial trials and rerun separately'
    report={'schema_version':1,'kind':'synthetic_provider_production_asgi_capacity',
            'completed_at_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':source_hashes,
            'limits':{'global_research':4,'global_provider':2},'provider_delay_seconds':args.provider_delay,
            'measurement_boundary':'ASGI in-process client+server RSS sampled every 10ms; Python heap includes tracemalloc overhead; nearest-rank latency quantiles; no network/provider/database-host production claim',
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'trials':results}
    with (args.output/'report.json').open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'trials':len(results),'report':str(args.output/'report.json')}))


if __name__=='__main__':
    main()
