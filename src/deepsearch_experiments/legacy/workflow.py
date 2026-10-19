"""Bounded local-document research workflow; no tracing or checkpoint storage."""
import logging
from uuid import uuid4


from deepsearch_tracing.context import notify, chunk_record


logger = logging.getLogger(__name__)




from deepsearch_agent.agents.contracts import Queries

from deepsearch_agent.agents.runtime import ResearchWorkflow

class LegacyResearchWorkflow(ResearchWorkflow):
    def research(self, state):
        if self.tool_session is not None:
            return super().research(state)
        decision = None
        if self.search_intent is not None and state['rounds'] == 0 and not state.get('input_evidence'):
            if self.on_progress:
                self.on_progress('retrieving')
            decision = self.search_intent.decide(state['question'], state['history'])
        if decision is not None and decision.action == 'search':
            queries = [decision.query]
        else:
            # answer/UNKNOWN cannot skip the first evidence search; use the
            # original planner on answer actions and policy failures.
            plan = self.call('Researcher', '围绕当前问题和证据缺口生成最多三个简短检索查询；补查时优先处理缺口。', Queries,
                             {'question': state['question'], 'history': state['history'], 'tasks': state['tasks'],
                              'instruction': state['instruction'], 'gaps': state.get('queries', []),
                              **({'evidence': self.context({**state, 'evidence': state['input_evidence']})}
                                 if state.get('input_evidence') else {})})
            queries = [q.strip()[:1000] for q in plan.queries if q.strip()]
        if not queries:
            queries = [state['question']]
        origin = 'policy' if decision is not None and decision.action == 'search' else 'researcher'
        notify(self.observer, 'search.plan', {'round': state['rounds'], 'origin': origin,
               'policy_call_id': getattr(decision, 'call_id', None), 'queries': queries})
        evidence = list(state['evidence'])
        visual_settings = getattr(self.visual_evidence, 'settings', None)
        image_refs = {(c.source_path, page) for c in evidence for page in c.visual_pages}
        keys = {c.chunk_id for c in evidence}
        fetch_count = min(state['top_k'] * 4, 20) if self.reranker else state['top_k']
        for query in dict.fromkeys(queries):
            search_id = uuid4().hex
            notify(self.observer, 'retrieval.request', {'search_id': search_id, 'query': query,
                   'origin': origin, 'policy_call_id': getattr(decision, 'call_id', None),
                   'round': state['rounds'], 'top_k': fetch_count})
            candidates = self.kb.search(query, top_k=fetch_count)
            if self.wiki_view is not None:
                try:
                    seen_candidates = {c.chunk_id for c in candidates}
                    hints = [c for c in self.wiki_view.evidence_hints(query, limit=min(state['top_k'], 5))
                             if c.chunk_id not in seen_candidates]
                    if hints:
                        candidates = hints[:1] + candidates + hints[1:]
                except Exception:
                    pass
            if self.observer is not None:
                notify(self.observer, 'retrieval.candidates', {'search_id': search_id,
                       'chunks': [chunk_record(c) for c in candidates]})
            if self.reranker and len(candidates) > 1:
                candidates = self.reranker.rerank(query, candidates, state['top_k'])
                notify(self.observer, 'rerank.result', {'search_id': search_id,
                       'status': getattr(self.reranker, 'last_status', 'unknown'),
                       'scores': getattr(self.reranker, 'last_scores', [])})
            if self.observer is not None:
                notify(self.observer, 'retrieval.result', {'search_id': search_id,
                       'chunks': [chunk_record(c) for c in candidates]})
            for c in candidates:
                key = c.chunk_id
                if key not in keys:
                    refs = {(c.source_path, page) for page in c.visual_pages}
                    if visual_settings is not None and len(image_refs | refs) > visual_settings.max_images:
                        continue
                    image_refs.update(refs)
                    keys.add(key)
                    evidence.append(c)
        return {'evidence': evidence, 'rounds': state['rounds'] + 1, 'supported': [], 'verified': False}
