"""Shared label-free search and bounded model calls for method comparisons."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import math
from threading import Timer
import time

from deepsearch_agent.knowledge.bm25 import BM25Index
from deepsearch_agent.core.models import LLMResponse, ModelServiceError
from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeLimits:
    k: int = 5
    model_calls: int = 24
    search_calls: int = 8
    token_budget: int = 128000
    max_output_tokens: int = 1024
    context_window: int = 32768
    deadline_seconds: float = 300.0

    def __post_init__(self):
        for name in ('k', 'model_calls', 'search_calls', 'token_budget', 'max_output_tokens', 'context_window'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f'{name} must be a positive integer')
        if isinstance(self.deadline_seconds, bool) or not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ValueError('deadline_seconds must be finite and positive')


class BM25Retriever:
    """Project BM25, explicitly NOT the author's Elasticsearch or dense index."""
    identity = {'name': 'project-bm25-v1', 'k1': 1.5, 'b': .75,
                'document_format': 'title-newline-text', 'tie_break': 'input-order'}

    def __init__(self, documents):
        self.documents = [{key: doc[key] for key in ('id', 'title', 'text')} for doc in documents]
        if not self.documents or len({doc['id'] for doc in self.documents}) != len(self.documents):
            raise ValueError('Retriever needs nonempty documents with unique IDs')
        self.index = BM25Index([doc['title'] + '\n' + doc['text'] for doc in self.documents])

    def search(self, query, k):
        return [dict(self.documents[i]) for i in self.index.rank(query, k)]


class ScriptedClient:
    """Offline plumbing checks only; scripted responses are not model results."""
    def __init__(self, responses):
        if not isinstance(responses, list) or any(not isinstance(item, str) for item in responses):
            raise ValueError('Scripted responses must be a list of strings')
        self.responses, self.position = list(responses), 0

    def generate(self, prompt):
        if self.position >= len(self.responses):
            raise ValueError('Scripted response tape exhausted')
        response = self.responses[self.position]
        self.position += 1
        return LLMResponse(text=response, model='scripted-offline')


class EvaluationRuntime:
    def __init__(self, client, retriever, limits, *, clock=time.monotonic):
        self.client, self.retriever, self.limits = client, retriever, limits
        self.clock, self.started = clock, clock()
        self.events = []
        self.retrieved_document_ids = []
        self.model_calls = self.search_calls = self.accounted_tokens = 0
        self.usage_missing_calls = self.known_tokens = 0
        self.token = CancellationToken()

    @contextmanager
    def cancellation_scope(self):
        # The existing gateway cancels an in-flight async HTTP request on this token.
        timer = Timer(self.limits.deadline_seconds, self.token.cancel)
        timer.daemon = True
        handle = current_token.set(self.token)
        timer.start()
        try:
            yield
        finally:
            timer.cancel()
            current_token.reset(handle)

    def _check(self):
        if self.token.event.is_set() or self.clock() - self.started >= self.limits.deadline_seconds:
            raise BudgetExceeded('deadline')

    def search(self, query):
        self._check()
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Search query must be nonempty')
        if self.search_calls >= self.limits.search_calls:
            raise BudgetExceeded('search_calls')
        self.search_calls += 1
        hits = self.retriever.search(query, self.limits.k)
        ids = [hit['id'] for hit in hits]
        self.retrieved_document_ids = list(dict.fromkeys([*self.retrieved_document_ids, *ids]))
        self.events.append({'kind': 'search', 'query': query, 'document_ids': ids})
        return hits

    def generate(self, prompt, *, stop=None):
        self._check()
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('Model prompt must be nonempty')
        if stop is not None and (not isinstance(stop, list) or any(not isinstance(s, str) or not s for s in stop)):
            raise ValueError('Invalid stop strings')
        if self.model_calls >= self.limits.model_calls:
            raise BudgetExceeded('model_calls')
        # A conservative byte-based preflight, NOT the provider's tokenizer.
        reservation = len(prompt.encode('utf-8')) + 512 + self.limits.max_output_tokens
        if reservation > self.limits.context_window:
            raise BudgetExceeded('context_window')
        if self.accounted_tokens + reservation > self.limits.token_budget:
            raise BudgetExceeded('token_budget')
        self.model_calls += 1
        self.accounted_tokens += reservation
        event = {'kind': 'model', 'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                 'reserved_tokens_estimate': reservation, 'reported_total_tokens': None,
                 'stop_mode': 'client_side_truncation' if stop else 'none'}
        self.events.append(event)
        started = self.clock()
        try:
            response = self.client.generate(prompt)
        except ModelServiceError as exc:
            event.update(status='error', error_type=type(exc).__name__, **exc.diagnostics())
            self._record_usage(event, exc, reservation)
            raise
        except BaseException as exc:
            self.usage_missing_calls += 1
            code = ('cancelled' if isinstance(exc, ResearchCancelled) else
                    'interrupted' if isinstance(exc, KeyboardInterrupt) else 'unexpected_error')
            event.update(status='error', error_type=type(exc).__name__, error_code=code)
            raise
        finally:
            event['latency_ms'] = max(0.0, (self.clock() - started) * 1000)
        event.update(model=response.model, request_id=response.request_id,
                     prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens)
        self._record_usage(event, response, reservation)
        text = response.text
        if stop:
            positions = [text.index(marker) for marker in stop if marker in text]
            if positions:
                text = text[:min(positions)]
        event.update(status='ok', response=text)
        self._check()
        if self.accounted_tokens > self.limits.token_budget:
            raise BudgetExceeded('token_budget_provider_overrun')
        return text

    def _record_usage(self, event, response, reservation):
        total = response.total_tokens
        if type(total) is int and total >= 0:
            self.known_tokens += total
            self.accounted_tokens += total - reservation
            event['reported_total_tokens'] = total
        else:
            self.usage_missing_calls += 1

    def statistics(self):
        return {'model_calls': self.model_calls, 'search_calls': self.search_calls,
                'accounted_tokens': self.accounted_tokens,
                'reported_total_tokens': self.known_tokens if not self.usage_missing_calls else None,
                'known_reported_tokens': self.known_tokens, 'usage_missing_calls': self.usage_missing_calls,
                'latency_ms': (self.clock() - self.started) * 1000,
                'limits': asdict(self.limits), 'token_budget_policy': 'utf8-byte-preflight-with-provider-reconciliation'}
