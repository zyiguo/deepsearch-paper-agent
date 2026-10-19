"""Bounded local-document research workflow; no tracing or checkpoint storage."""
import json
import logging
import re
from uuid import uuid4
from typing import TYPE_CHECKING

from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context
from pydantic import ValidationError

from deepsearch_agent.knowledge.reranker import Reranker
from deepsearch_tracing.context import notify, chunk_record

if TYPE_CHECKING:
    from deepsearch_agent.core.ports import VisualEvidenceProvider


logger = logging.getLogger(__name__)


def log_failure(state, reason):
    """One local terminal summary; never serialize model/user data or errors."""
    record = {'diagnostic_id': uuid4().hex[:12], 'reason': reason}
    for key in ('evidence', 'claims', 'supported', 'queries', 'issues', 'missing_answerable'):
        label = {'claims': 'claim', 'queries': 'query', 'issues': 'issue'}.get(key, key)
        record[f'{label}_count'] = len(state[key]) if key in state else None
    for key in ('rounds', 'reviews', 'dispatches'):
        record[key] = state.get(key)
    record['verified'] = state.get('verified')
    logger.warning('research_failure %s', json.dumps(record))


from deepsearch_agent.agents.contracts import ResearchState
from deepsearch_agent.agents.model_calls import ModelCallsMixin
from deepsearch_agent.agents.context import EvidenceContextMixin
from deepsearch_agent.agents.coordinator import CoordinatorMixin
from deepsearch_agent.agents.researcher import ResearcherMixin
from deepsearch_agent.agents.answering import AnsweringMixin
from deepsearch_agent.agents.path_answering import PathAnsweringMixin

class ResearchWorkflow(ModelCallsMixin, EvidenceContextMixin, CoordinatorMixin, ResearcherMixin, AnsweringMixin,
                       PathAnsweringMixin):
    def __init__(self, client, knowledge_base, context_window=8192, max_output_tokens=1024,
                 reranker: Reranker | None = None, on_progress=None, memory_context=None, search_intent=None, observer=None,
                 visual_evidence: 'VisualEvidenceProvider | None' = None, skills=None,
                 tool_session=None, mcp_settings=None, wiki_view=None, checkpointer=None, run_id=None,
                 budget_control=None, tools_factory=None, external_tools=None, evidence_policy=None):
        self.client, self.kb = client, knowledge_base
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.reranker = reranker
        self.on_progress = on_progress
        self.memory_context = memory_context
        self.search_intent = search_intent
        self.observer = observer
        self.visual_evidence = visual_evidence
        self.skills = skills
        self.tool_session = tool_session
        if tools_factory is None and hasattr(tool_session, 'tool_definitions'):
            from deepsearch_agent.adapters.tool_adapters import langgraph_tools
            tools_factory = langgraph_tools
        self.tools_factory = tools_factory
        self.external_tools = external_tools
        self.mcp_settings = mcp_settings
        self.wiki_view = wiki_view
        self.run_id = run_id
        self.budget_control = budget_control
        self.evidence_policy = evidence_policy
        self.role_repairs_remaining = {role: evidence_policy.repairs_per_role
            for role in ('Supervisor', 'Researcher', 'Answerer', 'Verifier')} if evidence_policy else {}
        self.schema_repairs_remaining = 1
        self.last_call_ids = {}
        graph = StateGraph(ResearchState)
        graph.add_node('supervisor', self.supervise)
        graph.add_node('researcher', self.research)
        graph.add_node('answerer', self.draft)
        graph.add_node('verifier', self.verify)
        graph.add_edge(START, 'supervisor')
        graph.add_conditional_edges('supervisor', self.route)
        graph.add_edge('researcher', 'supervisor')
        graph.add_edge('answerer', 'verifier')
        graph.add_edge('verifier', 'supervisor')
        self.graph = graph.compile(checkpointer=checkpointer)


    def answer(self, question, top_k, history, *, input_evidence=None, resume=False):
        from deepsearch_agent.core.answers import Answer, chunk_to_source_reference
        if self.evidence_policy:
            notify(self.observer, 'rag.policy', {'version': self.evidence_policy.version,
                   'child_tool_calls': self.evidence_policy.child_tool_calls,
                   'schema_repairs_per_role': self.evidence_policy.repairs_per_role,
                   'required_coverage_source': 'question_only', 'gold_used': False})
        try:
            # Explicitly disable hosted tracing, including inherited environment settings.
            with tracing_context(enabled=False):
                initial = None if resume else {'question': question, 'top_k': top_k, 'history': (history or [])[-6:],
                                           'rounds': 0, 'evidence': list(input_evidence or []),
                                           'input_evidence': list(input_evidence or []), 'claims': [], 'supported': [], 'queries': [],
                                           'tasks': [], 'instruction': '', 'issues': [], 'reviews': 0,
                                           'dispatches': 0, 'verified': False, 'stopped': False, 'missing_answerable': [],
                                           'requirements': [], 'answer_type': 'other',
                                           'coverage': [], 'coverage_complete': False,
                                           'review_notes': [], 'review_findings': [], 'review_action': '',
                                           'review_cache': {}, 'review_stalled': False,
                                           'plan_assumptions': [], 'plan_revision_pending': False}
                config = {'recursion_limit': 32, 'callbacks': []}
                if self.run_id:
                    config['configurable'] = {'thread_id': self.run_id}
                if resume and getattr(self.tool_session, 'execution', None) is not None:
                    saved = self.graph.get_state(config).values.get('tool_session_state')
                    if saved is not None:
                        self.tool_session.restore_runtime(saved)
                options = {'durability': 'sync'} if self.run_id else {}
                state = self.graph.invoke(initial, config=config, **options)
        except ValidationError:
            notify(self.observer, 'rag.outcome', {'status': 'schema_validation_failed'})
            log_failure({}, 'schema_validation_failed')
            return Answer('模型输出未通过格式校验，无法确认答案，请重试。', [])
        claims = [state['claims'][i] for i in state['supported']]
        ids = list(dict.fromkeys(ref for c in claims for ref in c['citations']))
        mapping = {old: new for new, old in enumerate(ids, 1)}
        lines = [re.sub(r'\[\d+\]', '', c['text']).strip() + ' ' + ''.join(f'[{mapping[r]}]' for r in dict.fromkeys(c['citations'])) for c in claims]
        if state.get('missing_answerable'):
            lines.append('以下问题项尚未完成核验回答：' + '；'.join(item['question'] for item in state['missing_answerable']) + '。')
        elif not lines or len(claims) < len(state['claims']) or state['queries'] or state['issues'] or state['stopped']:
            lines.append('现有证据不足或未通过核验，部分问题无法根据资料确认。')
        incomplete = (not claims or len(claims) < len(state['claims']) or state['queries']
                      or state['issues'] or state['stopped'] or state.get('missing_answerable'))
        if self.evidence_policy and not state.get('coverage_complete'):
            incomplete = True
            if not any('现有证据不足' in line for line in lines):
                lines.append('必要关系尚未全部获得证据覆盖，不能确认完整答案。')
        if self.evidence_policy and self.evidence_policy.structured_reviews:
            lines.extend('资料说明：' + note for note in state.get('review_notes', []))
        notify(self.observer, 'rag.outcome', {'status': 'incomplete' if incomplete else 'verified',
               'claims': state['claims'], 'supported': state['supported'],
               'queries': state['queries'], 'issues': state['issues'],
               'missing_answerable': state.get('missing_answerable', []),
               'stop_reason': state.get('stop_reason'),
               'evidence': [chunk_record(c) for c in state['evidence']] if self.observer is not None else []})
        if self.evidence_policy:
            notify(self.observer, 'rag.coverage', {'policy': self.evidence_policy.version,
                   'requirements': state.get('requirements', []), 'answer_type': state.get('answer_type', 'other'),
                   'coverage': state.get('coverage', []), 'complete': state.get('coverage_complete', False)})
            if self.evidence_policy.structured_reviews or self.evidence_policy.revisable_assumptions:
                notify(self.observer, 'rag.review', {'policy': self.evidence_policy.version,
                       'findings': state.get('review_findings', []), 'notes': state.get('review_notes', []),
                       'action': state.get('review_action', ''), 'stalled': state.get('review_stalled', False),
                       'assumptions': state.get('plan_assumptions', [])})
        if incomplete:
            reason = state.get('stop_reason')
            if not reason:
                if not state['verified']:
                    reason = 'stopped_after_research' if state['reviews'] else 'finish_before_verification'
                elif not state['claims']:
                    reason = 'empty_draft'
                elif not state['supported']:
                    reason = 'no_supported_claims'
                else:
                    reason = 'partial_answer'
            log_failure(state, reason)
        return Answer('\n\n'.join(lines), [chunk_to_source_reference(state['evidence'][i-1]) for i in ids],
                      skills_used=self.skills.used if self.skills else [])


    def route(self, state):
        return {'research': 'researcher', 'draft': 'answerer', 'revise': 'answerer',
                'finish': END, 'stop': END}[state['action']]

