"""Evaluation-only observers for production retrieval, tools, and model clients."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import re
from threading import RLock
import time

from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.agents.tool_service import PaperToolSession, ToolError


_TEXT_LIMIT = 150000
_PRIVATE_KEYS = {'authorization', 'api_key', 'apikey', 'access_token', 'refresh_token',
                 'password', 'secret', 'base64', 'headers'}
_TOOL_ERRORS = {'unknown_tool', 'invalid_arguments', 'tool_budget', 'tool_failed',
                'source_not_allowed', 'page_not_found', 'page_reader_unavailable',
                'invalid_page', 'ocr_unavailable', 'ocr_budget', 'image_budget',
                'evidence_not_found'}
_IMAGE_DATA = re.compile(r'data:image/[^;,\s]+;base64,[A-Za-z0-9+/=_-]+')


class RunTrace:
    """One question's trace and shared model-call budget, without gold labels."""

    def __init__(self, locations, max_model_calls=18):
        if type(max_model_calls) is not int or max_model_calls < 1:
            raise ValueError('max_model_calls must be a positive integer')
        self.locations = dict(locations)
        self.max_model_calls = max_model_calls
        self.model_calls = []
        self.retrieval_calls = []
        self.rerank_calls = []
        self.tool_calls = []
        self.events = []
        self._lock = RLock()
        self._secrets = set()
        self._model_attempts = 0
        self._started = time.perf_counter()
        self._stage = ContextVar('ablation_stage', default='unassigned')

    @contextmanager
    def stage(self, name):
        token = self._stage.set(str(name)[:120])
        try:
            yield
        finally:
            self._stage.reset(token)

    @property
    def current_stage(self):
        return self._stage.get()

    def register_secret(self, secret):
        if secret:
            with self._lock:
                self._secrets.add(secret)

    def safe(self, value):
        with self._lock:
            secrets = tuple(self._secrets)
        remaining = _TEXT_LIMIT

        def clean(item, depth=0):
            nonlocal remaining
            if item is None or type(item) in {bool, int}:
                return item
            if type(item) is float:
                return item if math.isfinite(item) else None
            if depth > 12 or remaining <= 0:
                return '[truncated]'
            if isinstance(item, str):
                result = _IMAGE_DATA.sub('[image omitted]', item)
                for secret in secrets:
                    result = result.replace(secret, '[redacted]')
                result = result[:min(remaining, _TEXT_LIMIT // 2)]
                remaining -= len(result)
                return result
            if isinstance(item, (tuple, list)):
                return [clean(part, depth + 1) for part in item[:1000]]
            if isinstance(item, dict):
                if item.get('type') in {'image', 'image_url'}:
                    return {'type': item['type'], 'omitted': True}
                return {
                    str(key)[:200]: '[redacted]' if str(key).lower() in _PRIVATE_KEYS
                    else clean(part, depth + 1)
                    for key, part in list(item.items())[:200]
                }
            return '[unsupported value]'

        return clean(value)

    def begin(self, collection, values):
        row = {**self.safe(values), 'stage': self.current_stage, 'status': 'pending',
               'started_ms': round((time.perf_counter() - self._started) * 1000, 3)}
        with self._lock:
            row['index'] = len(getattr(self, collection)) + 1
            getattr(self, collection).append(row)
        return row

    def finish(self, row, started, **values):
        with self._lock:
            row.update(self.safe(values))
            row['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)

    def reserve_model(self):
        with self._lock:
            if self._model_attempts >= self.max_model_calls:
                return False
            self._model_attempts += 1
            return True

    def ranking(self, chunks):
        rows, page_ids = [], []
        for rank, chunk in enumerate(chunks, 1):
            pages = [span['page_number'] for span in chunk.page_spans] or [chunk.page_number]
            pages = list(dict.fromkeys([*pages, *chunk.visual_pages]))
            ids = [self.locations[(chunk.source_path, page)] for page in pages
                   if (chunk.source_path, page) in self.locations]
            for page_id in ids:
                if page_id not in page_ids:
                    page_ids.append(page_id)
            rows.append({'rank': rank, 'source': chunk.source_path, 'page': chunk.page_number,
                         'page_end': chunk.page_end, 'pages': pages, 'page_ids': ids,
                         'chunk_id': chunk.chunk_id, 'content_id': chunk.content_id,
                         'evidence_kind': chunk.evidence_kind})
        return {'page_ids': page_ids, 'ranking': page_ids,
                'chunk_ids': [c.chunk_id for c in chunks], 'chunks': rows}

    def observer(self, kind, payload):
        if not isinstance(kind, str) or not kind.startswith(
            ('rag.', 'tool.', 'retrieval.', 'rerank.', 'research.', 'search.', 'skills.', 'answer.')
        ):
            return
        event = {'kind': kind, 'stage': self.current_stage, 'payload': self.safe(payload)}
        with self._lock:
            self.events.append(event)

    def to_dict(self):
        with self._lock:
            # Sanitize per record so earlier prompts do not consume later records' budgets.
            result = {name: [self.safe(row) for row in getattr(self, name)] for name in (
                'model_calls', 'retrieval_calls', 'rerank_calls', 'tool_calls', 'events')}
            result.update(max_model_calls=self.max_model_calls,
                          model_requests_started=self._model_attempts)
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


def _prompt(messages):
    parts = []
    image_count = 0
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                elif isinstance(block, dict) and block.get('type') in {'image', 'image_url'}:
                    image_count += 1
                    parts.append('[image omitted]')
    return '\n'.join(part for part in parts if isinstance(part, str)), image_count


def _role(prompt, stage):
    prefix = prompt[:500]
    role = re.search(r'\u4f60\u662f\s+(Supervisor|Researcher|Answerer|Verifier)\b', prefix)
    if role:
        return role.group(1)
    if '\u91cd\u6392' in prefix or stage == 'rerank':
        return 'Reranker'
    if 'ocr' in stage.lower():
        return 'OCR'
    return 'Model'


class MeasuredClient(OpenAICompatibleClient):
    def __init__(self, settings, trace, **kwargs):
        super().__init__(settings, **kwargs)
        self.trace = trace
        trace.register_secret(settings.api_key)

    def generate_messages(self, messages, *, structured=False):
        started = time.perf_counter()
        prompt, image_count = _prompt(messages)
        row = self.trace.begin('model_calls', {
            'role': _role(prompt, self.trace.current_stage), 'prompt': prompt,
            'image_count': image_count, 'structured': structured, 'model': self.settings.model,
            'request_id': None, 'prompt_tokens': None, 'completion_tokens': None,
            'total_tokens': None, 'raw_output': '',
        })
        if not self.trace.reserve_model():
            self.trace.finish(row, started, status='budget_exceeded', error_code='model_call_budget')
            raise ModelServiceError('model_call_budget')
        try:
            result = super().generate_messages(messages, structured=structured)
        except Exception as error:
            self.trace.finish(row, started, status='error', error_code='model_service_error'
                              if isinstance(error, ModelServiceError) else 'model_call_failed',
                              status_code=getattr(error, 'status_code', None))
            raise
        self.trace.finish(row, started, status='ok', raw_output=result.text, model=result.model,
                          request_id=result.request_id, prompt_tokens=result.prompt_tokens,
                          completion_tokens=result.completion_tokens, total_tokens=result.total_tokens)
        return result


class TracedKnowledgeBase:
    def __init__(self, kb, trace):
        self._kb, self.trace = kb, trace

    def __getattr__(self, name):
        return getattr(self._kb, name)

    def restrict_sources(self, sources):
        return type(self)(self._kb.restrict_sources(sources), self.trace)

    def search(self, query, top_k=5, **kwargs):
        started = time.perf_counter()
        row = self.trace.begin('retrieval_calls', {
            'query': query, 'top_k': top_k, 'requested_k': top_k})
        try:
            with self.trace.stage('retrieval'):
                chunks = self._kb.search(query, top_k=top_k, **kwargs)
        except Exception:
            self.trace.finish(row, started, status='error', error_code='retrieval_failed')
            raise
        self.trace.finish(row, started, status='ok', **self.trace.ranking(chunks))
        return chunks


class TracedReranker:
    def __init__(self, reranker, trace):
        self._reranker, self.trace = reranker, trace

    def __getattr__(self, name):
        return getattr(self._reranker, name)

    def rerank(self, query, chunks, top_k):
        started = time.perf_counter()
        before = self.trace.ranking(chunks)
        row = self.trace.begin('rerank_calls', {
            'query': query, 'top_k': top_k, 'requested_k': top_k,
            **{f'initial_{key}': value for key, value in before.items()},
        })
        try:
            with self.trace.stage('rerank'):
                result = self._reranker.rerank(query, chunks, top_k)
        except Exception:
            self.trace.finish(row, started, status='error', error_code='rerank_failed')
            raise
        self.trace.finish(row, started, status=getattr(self._reranker, 'last_status', 'ok'),
                          scores=getattr(self._reranker, 'last_scores', []),
                          **self.trace.ranking(result))
        return result


class TracedToolSession(PaperToolSession):
    def __init__(self, *args, trace, **kwargs):
        self.trace = trace
        super().__init__(*args, **kwargs)

    def invoke(self, name, arguments):
        started = time.perf_counter()
        previous = {chunk.chunk_id for chunk in self.evidence}
        row = self.trace.begin('tool_calls', {'name': name, 'arguments': arguments})
        try:
            with self.trace.stage(f'tool:{name}'):
                result = super().invoke(name, arguments)
        except Exception as error:
            code = str(error) if isinstance(error, ToolError) and str(error) in _TOOL_ERRORS else 'tool_failed'
            self.trace.finish(row, started, status='error', error_code=code,
                              new_chunk_ids=[], chunk_ids=[], page_ids=[], ranking=[])
            raise
        self.trace.finish(row, started, status='ok', error_code=None, data=result.data,
                          content=result.content,
                          new_chunk_ids=[c.chunk_id for c in self.evidence if c.chunk_id not in previous],
                          **self.trace.ranking(result.chunks))
        return result
