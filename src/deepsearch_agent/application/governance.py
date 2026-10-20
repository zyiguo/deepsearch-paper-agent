"""Single-deployment metrics, quota accounting, alert evaluation and admission."""
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
import json
import logging
import math
from threading import Event, RLock, Thread
import time

from deepsearch_agent.core.governance import GovernanceDenied, current_usage_scope, reported_provider_usage
from deepsearch_agent.core.models import LLMResponse, optional_count
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolNotExecuted
from deepsearch_agent.core.cancellation import current_token, ResearchCancelled

log = logging.getLogger('deepsearch.governance')
BUCKETS = (.01,.05,.1,.5,1,5,15,30,60,120,300,float('inf'))


class Metrics:
    """Bounded dimensions; identities and submitted content are never labels."""
    def __init__(self):
        self.lock = RLock()
        self.counters = Counter()
        self.histograms = {}

    def inc(self, name, labels=(), amount=1):
        with self.lock:
            key = (name, tuple(labels))
            if key in self.counters or len(self.counters)<512:
                self.counters[key] += amount

    def observe(self, name, labels, value):
        with self.lock:
            key = (name,tuple(labels))
            if key not in self.histograms and len(self.histograms)>=256:
                return
            row = self.histograms.setdefault(key, {'buckets':[0]*len(BUCKETS),'sum':0.,'count':0})
            row['count'] += 1
            row['sum'] += value
            for i,bound in enumerate(BUCKETS):
                if value<=bound:
                    row['buckets'][i] += 1

    @staticmethod
    def labels(values):
        return '{' + ','.join(name+'='+json.dumps(str(value),ensure_ascii=True) for name,value in values) + '}' if values else ''

    def render(self):
        with self.lock:
            lines, seen = [], set()
            for (name,labels),value in sorted(self.counters.items()):
                if name not in seen:
                    lines.append('# TYPE '+name+' counter')
                    seen.add(name)
                lines.append(name+self.labels(labels)+' '+str(value))
            for (name,labels),row in sorted(self.histograms.items()):
                if name not in seen:
                    lines.append('# TYPE '+name+' histogram')
                    seen.add(name)
                for bound,count in zip(BUCKETS,row['buckets']):
                    lines.append(name+'_bucket'+self.labels((*labels,('le','+Inf' if bound==float('inf') else bound)))+' '+str(count))
                lines.extend([name+'_sum'+self.labels(labels)+' '+str(row['sum']),
                              name+'_count'+self.labels(labels)+' '+str(row['count'])])
        return '\n'.join(lines)+'\n'


class Governance:
    def __init__(self, store, settings, *, clock=time.monotonic, backlog_reader=None):
        self.store, self.settings, self.clock = store, settings, clock
        self.metrics = Metrics()
        self.lock = RLock()
        self.active, self.peaks = Counter(), Counter()
        self.recent = deque(maxlen=10000)
        self.latencies = {'provider_duration':deque(maxlen=1000),'research_duration':deque(maxlen=1000)}
        self.workspaces = {}
        self.redis_backends = {}
        self.backlog_reader = backlog_reader
        self.stopped = Event()
        self.thread = None
        self.store.recover()

    def record(self, name, **labels):
        try:
            self.metrics.inc('deepsearch_'+name+'_total',sorted(labels.items()))
        except Exception:
            log.warning('metric_recording_unavailable')

    def duration(self, name, seconds, **labels):
        try:
            self.metrics.observe('deepsearch_'+name+'_seconds',sorted(labels.items()),max(0,seconds))
            if name in self.latencies:
                with self.lock:
                    self.latencies[name].append((self.clock(),max(0,seconds)))
        except Exception:
            log.warning('metric_recording_unavailable')

    def event(self, category, outcome):
        self.record(category, outcome=outcome)
        with self.lock:
            self.recent.append((self.clock(),category,outcome))

    @contextmanager
    def resource(self, kind):
        if kind not in {'research','ingestion','provider'}:
            raise ValueError('Unsupported resource kind')
        scope = current_usage_scope.get()
        keys = [(kind,'global','all'),(kind,'workspace',scope.workspace),(kind,'user',scope.user)]
        with self.lock:
            for resource,level,subject in keys:
                if self.active[(resource,level,subject)]>=getattr(self.settings,level+'_'+resource):
                    self.record('resource_rejection',kind=resource,level=level)
                    self.event('admission', 'rejected')
                    raise GovernanceDenied('capacity_'+level+'_'+resource,503)
            for key in keys:
                self.active[key] += 1
                if key[1]=='global':
                    self.peaks[key] = max(self.peaks[key],self.active[key])
        try:
            yield
        finally:
            with self.lock:
                for key in keys:
                    self.active[key] -= 1
                    if not self.active[key]:
                        del self.active[key]

    def invoke(self, kind, model, estimate, call):
        if current_token.get() is not None:
            current_token.get().check()
        started = self.clock()
        scope = current_usage_scope.get()
        with self.resource('provider'):
            try:
                ident = self.store.reserve(scope,kind,model,estimate,getattr(self.settings,kind+'_unit_rate'),self.settings)
            except GovernanceDenied as error:
                self.record('quota_rejection',scope='user' if error.code.startswith('user_') else 'workspace')
                self.event('quota','rejected')
                raise
            except Exception:
                raise GovernanceDenied('quota_store_unavailable',503) from None
            samples = []
            token = reported_provider_usage.set(samples)
            outcome = 'unknown'
            try:
                try:
                    self.store.dispatch(ident)
                except Exception:
                    raise GovernanceDenied('quota_store_unavailable',503) from None
                try:
                    result = call()
                except BaseException as error:
                    total = optional_count(getattr(error,'total_tokens',None))
                    not_executed = isinstance(error,(ValueError,ToolNotExecuted)) and not isinstance(error,ToolExecutionBlocked)
                    # ModelServiceError is a RuntimeError; provider/transport errors
                    # without usage always stay unknown, including cancellation.
                    self._finish(ident,tokens=total,not_executed=not_executed)
                    outcome = 'not_executed' if not_executed else 'cancelled' if isinstance(error,ResearchCancelled) else 'error'
                    raise
                if isinstance(result,LLMResponse):
                    total = optional_count(result.total_tokens)
                    if total is None and optional_count(result.prompt_tokens) is not None and optional_count(result.completion_tokens) is not None:
                        total = result.prompt_tokens+result.completion_tokens
                else:
                    total = sum(samples) if samples and all(v is not None for v in samples) else None
                status = self._finish(ident,tokens=total)
                outcome = 'success' if status=='settled' else 'usage_unknown'
                if total is not None:
                    try:
                        self.metrics.inc('deepsearch_provider_reported_tokens_total',[('kind',kind)],total)
                    except Exception:
                        log.warning('metric_recording_unavailable')
                return result
            finally:
                reported_provider_usage.reset(token)
                self.event('provider',outcome)
                self.duration('provider_duration',self.clock()-started,kind=kind,outcome=outcome)

    def _finish(self, ident, **kwargs):
        try:
            return self.store.finish(ident,**kwargs)
        except Exception:
            raise ToolExecutionBlocked('quota_settlement_unavailable') from None

    def register_workspace(self, ident, ledger, redis=None):
        with self.lock:
            self.workspaces[ident] = ledger
            if redis is not None:
                self.redis_backends[ident] = redis

    def snapshot(self):
        summary = self.store.summary()
        with self.lock:
            recent = [(category,outcome) for when,category,outcome in self.recent if self.clock()-when<=300]
            resources = {kind:{'active':self.active[(kind,'global','all')],
                              'peak':self.peaks[(kind,'global','all')], 'limit':getattr(self.settings,'global_'+kind)}
                         for kind in ('research','ingestion','provider')}
            ledgers = list(self.workspaces.values())
            latency = {}
            for kind in ('provider','research'):
                values=sorted(seconds for when,seconds in self.latencies[kind+'_duration'] if self.clock()-when<=300)
                latency[kind]={'samples':len(values),'p95_seconds':values[math.ceil(len(values)*.95)-1] if values else None}
            degraded_redis = any(getattr(b,'status',None)=='unavailable' for b in self.redis_backends.values())
        unknown_tools, oldest_tool = 0, 0
        for ledger in ledgers:
            row = ledger.summary()
            unknown_tools += row['unknown_count']
            oldest_tool = max(oldest_tool,row['oldest_unknown_seconds'])
        if self.backlog_reader is not None:
            backlog = self.backlog_reader()
            unknown_tools,oldest_tool = backlog['unknown_count'],backlog['oldest_unknown_seconds']
        summary.update(unknown_tool_count=unknown_tools, oldest_unknown_tool_seconds=oldest_tool)
        conditions = {'unknown_cost_stale':summary['oldest_unknown_seconds']>=self.settings.unknown_alert_seconds,
                      'unknown_tool_stale':oldest_tool>=self.settings.unknown_alert_seconds,
                      'cost_reservation_overrun':summary['reservation_overruns']>0,
                      'capacity_rejections':('admission','rejected') in recent,
                      'quota_rejections':('quota','rejected') in recent,
                      'capacity_saturation':any(r['active']>=r['limit'] for r in resources.values()),
                      'redis_degraded':degraded_redis}
        for category in ('research','ingestion','provider'):
            outcomes = [outcome for kind,outcome in recent if kind==category]
            failures = sum(o in {'error','interrupted','failed'} for o in outcomes)
            conditions[category+'_failure_ratio'] = len(outcomes)>=self.settings.failure_min_samples and failures/len(outcomes)>=self.settings.failure_ratio
        for kind,row in latency.items():
            conditions[kind+'_latency_high']=row['samples']>=self.settings.failure_min_samples and row['p95_seconds']>=getattr(self.settings,'slow_'+kind+'_seconds')
        alerts, changes = self.store.alerts(conditions,hold_seconds=self.settings.alert_for_seconds)
        for change in changes:
            if change['state']!='resolved':
                log.warning(json.dumps({'event':'governance_alert',**change}))
        return {'expenses':summary,'resources':resources,'latency':latency,'alerts':alerts,'window_seconds':300}

    def render(self):
        snapshot = self.snapshot()
        text = self.metrics.render()
        for name,value in snapshot['expenses'].items():
            text += '# TYPE deepsearch_'+name+' gauge\ndeepsearch_'+name+' '+str(value)+'\n'
        for name in ('active','peak','limit'):
            text += '# TYPE deepsearch_resource_'+name+' gauge\n'
            for kind,row in snapshot['resources'].items():
                text += 'deepsearch_resource_'+name+'{kind="'+kind+'"} '+str(row[name])+'\n'
        text += '# TYPE deepsearch_alert_firing gauge\n'
        for alert in snapshot['alerts']:
            text += 'deepsearch_alert_firing{name='+json.dumps(alert['name'])+'} '+str(int(alert['state']=='firing'))+'\n'
        text += '# TYPE deepsearch_rolling_latency_p95_seconds gauge\n'
        for kind,row in snapshot['latency'].items():
            if row['p95_seconds'] is not None:
                text += 'deepsearch_rolling_latency_p95_seconds{kind="'+kind+'"} '+str(row['p95_seconds'])+'\n'
        return text

    def start(self):
        def evaluate():
            while not self.stopped.wait(5):
                try:
                    self.snapshot()
                except Exception:
                    log.warning('governance_alert_evaluation_unavailable')
        self.thread = Thread(target=evaluate,daemon=True,name='governance-alerts')
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
