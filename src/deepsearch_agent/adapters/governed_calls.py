"""Provider instrumentation at the real invocation boundary, not graph replay."""
from functools import wraps
from contextvars import copy_context
from threading import Lock
import time

from deepsearch_agent.core.governance import current_governance, report_provider_usage


def metered(kind):
    def decorate(function):
        @wraps(function)
        def invoke(self, *args, **kwargs):
            service = current_governance.get()
            if service is None:
                return function(self,*args,**kwargs)
            value = args[0] if args else kwargs.get('messages',kwargs.get('texts',kwargs.get('inputs',[])))
            if not value:
                # Empty embedding batches make no HTTP request. LLM validation
                # still raises its original argument error, before accounting.
                return function(self,*args,**kwargs)
            if kind=='llm':
                text, images = [], 0
                for message in value:
                    content = message.get('content','')
                    if isinstance(content,str):
                        text.append(content)
                    elif isinstance(content,list):
                        text.extend(part.get('text','') for part in content if part.get('type')=='text')
                        images += sum(part.get('type')=='image_url' for part in content)
                estimate = sum(len(t.encode('utf-8')) for t in text) + len(value)*64 + self.settings.max_output_tokens + images*service.settings.image_reserve_tokens
            elif kind=='embedding':
                estimate = sum(len(t.encode('utf-8'))+16 for t in value)
            else:
                estimate = sum(len(item.get('text','').encode('utf-8'))+16 if 'text' in item else service.settings.image_reserve_tokens for item in value)
            return service.invoke(kind,self.settings.model,max(1,estimate),lambda:function(self,*args,**kwargs))
        return invoke
    return decorate


class GovernedExecutor:
    """Hold a deployment admission lease while an ingestion job is queued/running."""
    def __init__(self, delegate):
        self.delegate = delegate

    def submit(self, function, *args, **kwargs):
        service = current_governance.get()
        context = copy_context()
        guard = service.resource('ingestion') if service is not None else None
        if guard:
            guard.__enter__()
        release_lock, released = Lock(), False
        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    if guard:
                        guard.__exit__(None,None,None)
        def execute():
            started, outcome = time.monotonic(), 'interrupted'
            try:
                result = function(*args,**kwargs)
                owner = getattr(function,'__self__',None)
                outcome = 'completed'
                if owner is not None and hasattr(owner,'records'):
                    with owner.lock:
                        outcome = 'completed' if owner.records[args[0]]['status']=='ready' else 'failed'
                return result
            finally:
                if service:
                    service.event('ingestion',outcome)
                    service.duration('ingestion_duration',time.monotonic()-started,outcome=outcome)
                release()
        try:
            future = self.delegate.submit(context.run,execute)
            def on_done(result):
                if result.cancelled():
                    if service:
                        service.event('ingestion','cancelled')
                    release()
            future.add_done_callback(on_done)
            return future
        except BaseException:
            release()
            raise

    def shutdown(self, **kwargs):
        return self.delegate.shutdown(**kwargs)
