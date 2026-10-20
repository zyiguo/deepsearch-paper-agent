"""Real process loss across quota, synthetic provider and tool-result commits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.governance import usage_context
from deepsearch_agent.core.models import LLMResponse
from deepsearch_agent.core.tool_execution import ToolKind, ToolExecutionBlocked


def count(root):
    with sqlite3.connect(root/'provider.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS effects(id INTEGER PRIMARY KEY)')
        return db.execute('SELECT count(*) FROM effects').fetchone()[0]


def worker(root, fault):
    g=Governance(GovernanceStore(root/'cost.sqlite3'),GovernanceSettings(user_daily_units=10000,workspace_daily_units=10000))
    ledger=SQLiteToolLedger(root/'tool.sqlite3'); ledger.begin_run('synthetic-run'); ledger.recover_interrupted()
    original=g.store.dispatch
    def dispatch(ident):
        if fault=='reserved':
            os._exit(73)
        original(ident)
        if fault=='dispatched':
            os._exit(73)
    g.store.dispatch=dispatch
    def provider():
        with sqlite3.connect(root/'provider.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS effects(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO effects DEFAULT VALUES')
        if fault=='provider_effect':
            os._exit(73)
        return LLMResponse('completed',total_tokens=10)
    def ledger_fault(stage,ident):
        if (stage,fault) in {('after_effect','cost_settled'),('after_commit','tool_committed')}:
            os._exit(73)
    ex=ToolExecutor(ledger,'synthetic-run',fault=ledger_fault)
    status='blocked'
    with usage_context(g,user='synthetic-user',workspace='synthetic-workspace',run_id='synthetic-run'):
        try:
            result=ex.execute('logical-slot','synthetic-paid',ToolKind.PAID,{},
                lambda ident:{'text':g.invoke('llm','synthetic',8000,provider).text})
            status='replayed' if result.replayed else 'executed'
        except ToolExecutionBlocked as error:
            assert error.code=='tool_outcome_unknown',error.code
    with g.store.connect() as db:
        invoices=[dict(r) for r in db.execute('SELECT status,reserved,actual,operation_id FROM cost_reservations')]
    print(json.dumps({'status':status,'provider_effects':count(root),'costs':invoices,
                      'used_or_held':g.store.usage('user','synthetic-user',10000)['used_or_held']}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--fault',default='')
    args=parser.parse_args()
    if args.worker:
        worker(args.output,args.fault)
        return
    args.output.mkdir(parents=True,exist_ok=False)
    results=[]
    expected={'reserved':('released',0,0),'dispatched':('unknown',0,8000),
              'provider_effect':('unknown',1,8000),'cost_settled':('settled',1,10),
              'tool_committed':('settled',1,10)}
    for stage,(cost_status,effects,used) in expected.items():
        root=args.output/stage; root.mkdir()
        command=[sys.executable,'-B',str(Path(__file__).resolve()),'--worker','--output',str(root.resolve())]
        crashed=subprocess.run([*command,'--fault',stage],capture_output=True,text=True,encoding='utf-8',timeout=30)
        assert crashed.returncode==73,crashed.stderr
        resumed=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=30)
        assert resumed.returncode==0,resumed.stderr
        result=json.loads(resumed.stdout)
        assert len(result['costs'])==1 and result['costs'][0]['status']==cost_status,result
        assert result['provider_effects']==effects and result['used_or_held']==used,result
        assert result['status']==('replayed' if stage=='tool_committed' else 'blocked'),result
        results.append({'stage':stage,'crash_exit':73,'result':result,'passed':True})
    report={'schema_version':1,'kind':'synthetic_provider_quota_process_faults','cases':results,
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with (args.output/'report.json').open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':len(results),'report':str(args.output/'report.json')}))


if __name__=='__main__':
    main()
