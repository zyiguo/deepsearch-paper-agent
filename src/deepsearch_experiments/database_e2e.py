"""Opt-in Docker acceptance with real PostgreSQL, Redis and synthetic team users."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import time

import httpx
from deepsearch_experiments.infrastructure_e2e import Acceptance


class DatabaseAcceptance(Acceptance):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.password = secrets.token_hex(20)

    def configuration(self):
        import yaml
        config = super().configuration()
        overlay = yaml.safe_load((self.root/'compose.database.yaml').read_text(encoding='utf-8'))
        postgres = overlay['services']['postgres']
        postgres['environment']['POSTGRES_PASSWORD'] = secrets.token_hex(20)
        postgres['environment']['POSTGRES_APP_PASSWORD'] = self.password
        postgres['ports'] = ['127.0.0.1::5432']
        postgres['restart'] = 'no'
        postgres['volumes'][1] = str(self.root/'deploy/postgres/001-role.sql')+':/docker-entrypoint-initdb.d/001-role.sql:ro'
        config['services']['postgres'] = postgres
        config['volumes']['postgres-data'] = {}
        api = config['services']['api']
        api['environment'].update(DATABASE_BACKEND='postgresql', DATABASE_URL=f'postgresql://deepsearch:{self.password}@postgres:5432/deepsearch',
                                  ACCESS_MODE='team',ACCESS_COOKIE_SECURE='false',API_RATE_LIMIT='0')
        api['depends_on'] = {'postgres':{'condition':'service_healthy'}}
        return config

    def setup(self):
        self.file.write_text(json.dumps(self.configuration(),indent=2),encoding='utf-8')
        self.compose_command(['config','--quiet'])
        self.record('engine',version=self.run_command(['version','--format','{{.Server.Version}}']).strip())
        self.compose_command(['--progress','plain','build','--pull=false','api'],timeout=900)
        self.built = True
        self.record('image_built',image_id=self.run_command(['image','inspect',self.image,'--format','{{.Id}}']).strip())
        self.created = True
        self.compose_command(['up','-d','postgres','redis'],timeout=180)
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            state = json.loads(self.compose_command(['ps','--format','json','postgres']))
            if state.get('Health')=='healthy':
                break
            time.sleep(1)
        else:
            raise RuntimeError('PostgreSQL did not become healthy')
        self.compose_command(['run','--rm','--no-deps','-T','api','python','-m','deepsearch_agent.application.database_cli','upgrade'])
        self.compose_command(['up','-d','api'],timeout=180)
        self.refresh_endpoint()
        self.wait_ready('ok')
        assert self.compose_command(['exec','-T','api','id','-u']).strip()=='10001'
        version = self.compose_command(['exec','-T','postgres','psql','-U','postgres','-d','deepsearch','-Atc','SHOW server_version']).strip()
        self.record('postgres_ready',server_version=version,non_root_api=True,explicit_schema_upgrade=True)

    def verify(self):
        script = """from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.persistence.business import build_database,access_store
from deepsearch_agent.application.access import AccessService
db=build_database(DatabaseSettings.load())
try: AccessService(access_store(db,None),AccessSettings.load()).create_user('fixture-admin','Fixture Admin','Synthetic-test-password-20261018',admin=True)
finally: db.close()
print('Fixture account provisioned in PostgreSQL')
"""
        self.compose_command(['exec','-T','api','python','-'],input=script)
        with httpx.Client(trust_env=False,headers={'X-DeepSearch-Request':'1'}) as admin, httpx.Client(trust_env=False,headers={'X-DeepSearch-Request':'1'}) as member:
            def call(client,method,path,expected=200,**kwargs):
                response = client.request(method,self.base+path,timeout=20,**kwargs)
                assert response.status_code==expected,(path,response.status_code)
                assert response.headers.get('x-request-id')
                return response.json()
            login = {'username':'fixture-admin','password':'Synthetic-test-password-20261018'}
            call(member,'GET','/api/workspaces',401)
            call(admin,'POST','/api/access/login',json=login)
            call(admin,'POST','/api/admin/users',201,json={'username':'fixture-member','display_name':'Fixture Member','password':login['password']})
            alpha = call(admin,'POST','/api/workspaces',201,json={'name':'Alpha'})['id']
            prefix = '/w/'+alpha+'/api'
            call(admin,'POST',prefix+'/demo/ingest')
            sid = call(admin,'POST',prefix+'/sessions',201,json={'title':'PostgreSQL fixture'})['id']
            turn = call(admin,'POST',prefix+'/sessions/'+sid+'/turns',201,json={'question':'模型怎么训练？'})
            call(admin,'PATCH',prefix+'/sessions/'+sid+'/memory',json={'research_goal':'Synthetic persisted goal'})
            call(member,'POST','/api/access/login',json={**login,'username':'fixture-member'})
            call(member,'GET',prefix+'/sessions',404)
            beta = call(member,'POST','/api/workspaces',201,json={'name':'Beta'})['id']
            assert call(member,'GET','/w/'+beta+'/api/sessions')==[]
            call(admin,'PUT','/api/workspaces/'+alpha+'/members',json={'username':'fixture-member','role':'viewer'})
            assert call(member,'GET',prefix+'/sessions/'+sid)['turns'][0]['answer']==turn['answer']
            call(member,'POST',prefix+'/sessions',403,json={'title':'denied'})
            self.record('postgres_team_http',account_and_membership=True,session_and_answer=True,memory=True,workspace_isolation=True,viewer_write_denied=True)
            pgport = self.compose_command(['port','postgres','5432']).strip().rsplit(':',1)[1]
            redisport = self.compose_command(['port','redis','6379']).strip().rsplit(':',1)[1]
            env = {**os.environ,'TEST_DATABASE_URL':f'postgresql://deepsearch:{self.password}@127.0.0.1:{pgport}/deepsearch',
                   'TEST_REDIS_URL':f'redis://127.0.0.1:{redisport}/15'}
            result = self.run_command([sys.executable,'-m','pytest','tests','-q','--basetemp',str(self.output/'pytest-temp')],
                                      docker=False,env=env,timeout=300)
            self.record('host_full_regression_real_postgres_redis',summary=result.strip().splitlines()[-1])
            self.compose_command(['restart','api'])
            self.refresh_endpoint();self.wait_ready('ok')
            saved = call(admin,'GET',prefix+'/sessions/'+sid)
            assert saved['turns'][0]['answer']==turn['answer'] and saved['memory']['research_goal']=='Synthetic persisted goal'
            self.record('api_restart',postgres_session_and_memory_preserved=True)
            self.compose_command(['stop','postgres'])
            call(admin,'GET','/api/health/ready',503)
            call(admin,'GET','/api/workspaces',503)
            call(admin,'GET','/api/health/live')
            self.record('database_outage',readiness_503=True,business_503=True,liveness_200=True,no_sqlite_fallback=True)
            self.compose_command(['start','postgres'])
            deadline = time.monotonic()+90
            while time.monotonic()<deadline:
                try:
                    self.wait_ready('ok',timeout=5)
                    break
                except RuntimeError:
                    time.sleep(.5)
            else:
                raise RuntimeError('Database recovery timed out')
            saved = call(admin,'GET',prefix+'/sessions/'+sid)
            assert saved['turns'][0]['answer']==turn['answer']
            self.record('database_recovery',preserved_data=True)
            check = """from pathlib import Path
p=Path('/app/data/team')
assert not (p/'identity.sqlite3').exists()
assert not list(p.glob('workspaces/*/conversations.sqlite3'))
assert not list(p.glob('workspaces/*/research_runs.sqlite3'))
print('Business records were stored in PostgreSQL; local checkpoints/indexes stay separate')
"""
            self.compose_command(['exec','-T','api','python','-'],input=check)
            self.record('storage_boundary',no_shadow_sqlite_business_files=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--docker-host')
    parser.add_argument('--docker-config',type=Path)
    parser.add_argument('--build-proxy')
    args = parser.parse_args(argv)
    run = DatabaseAcceptance(Path(__file__).resolve().parents[2],args.output,docker_host=args.docker_host,
                             docker_config=args.docker_config,build_proxy=args.build_proxy)
    error = None
    try:
        run.setup();run.verify()
    except Exception as exc:
        # Logs contain only synthetic input. Report never includes a DSN or password.
        error = {'type':type(exc).__name__,'message':'Acceptance failed; inspect the numbered local command logs'}
    finally:
        try: run.cleanup()
        except Exception as exc: error=error or {'type':type(exc).__name__,'message':'Cleanup failed; inspect project labels'}
        report = {'status':'failed' if error else 'passed','finished_at':datetime.now(timezone.utc).isoformat(),
                  'project':run.project,'synthetic_inputs':True,'cloud_model_calls':False,'steps':run.steps,'error':error}
        (run.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'report':str(run.output/'report.json')},ensure_ascii=False),flush=True)
    return int(error is not None)


if __name__=='__main__':
    raise SystemExit(main())
