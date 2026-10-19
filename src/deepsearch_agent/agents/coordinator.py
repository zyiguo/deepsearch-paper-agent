"""Bounded local-document research workflow; no tracing or checkpoint storage."""
import json
from uuid import uuid4


from deepsearch_agent.core.tokens import estimate_tokens
from deepsearch_tracing.context import notify, event_scope
from deepsearch_agent.agents.subagent_contracts import compact_results


from deepsearch_agent.agents.contracts import Decision, ControlledDecision, RevisableDecision

class CoordinatorMixin:
    def supervise(self, state):
        policy = self.evidence_policy
        if policy:
            complete = (state['verified'] and state.get('coverage_complete') and bool(state['claims'])
                        and len(state['supported']) == len(state['claims'])
                        and not any(state.get(k) for k in ('queries', 'issues', 'missing_answerable', 'citation_errors')))
            action = ('finish' if complete else 'draft'
                      if state['rounds'] > 0 and not state['verified'] and state['evidence']
                      and not state.get('plan_revision_pending')
                      and state['reviews'] < 3 and state['dispatches'] < 8 else '')
            if action:
                notify(self.observer, 'communication.parent_decision', {
                    'decision_source': 'evidence_policy', 'action': action,
                    'stop_reason': 'verified_coverage' if complete else 'probe_after_research'})
                return {'action': action, 'dispatches': state['dispatches'] + 1,
                        'instruction': 'Answer the original question using existing evidence; verify every required bridge.',
                        'stopped': False, 'stop_reason': ''}
        control = getattr(self, 'budget_control', None)
        reason = control.research_stop() if control is not None else ''
        if reason:
            complete = (state['verified'] and bool(state['claims'])
                        and len(state['supported']) == len(state['claims'])
                        and not any(state.get(key) for key in
                                    ('queries', 'issues', 'missing_answerable', 'citation_errors'))
                        and (not policy or state.get('coverage_complete')))
            if complete:
                action = 'finish'
            elif (not state['verified'] and state['evidence'] and state['reviews'] < 3
                  and not state.get('plan_revision_pending')
                  and state['dispatches'] < 8):
                action = 'draft'
            else:
                action = 'stop'
            notify(self.observer, 'budget.finalize', {'reason': reason, 'action': action,
                   'budget': control.snapshot()})
            notify(self.observer, 'communication.parent_decision', {
                'decision_source': 'budget_guard', 'action': action, 'stop_reason': reason,
                'task_ids': [r['task_id'] for r in state.get('subtask_results', [])],
            })
            return {'action': action, 'dispatches': state['dispatches'] + 1,
                    'stopped': action == 'stop', 'stop_reason': reason,
                    'instruction': 'Research has ended. Answer only what existing evidence supports; '
                                   'retain uncertainty and do not bypass independent verification.'}
        if state['dispatches'] >= 8:
            notify(self.observer, 'communication.parent_decision', {
                'decision_source': 'dispatch_guard', 'action': 'stop', 'stop_reason': 'dispatch_limit',
                'task_ids': [r['task_id'] for r in state.get('subtask_results', [])],
            })
            return {'action': 'stop', 'stopped': True, 'stop_reason': 'dispatch_limit'}
        if policy and policy.structured_reviews:
            if state.get('review_stalled'):
                notify(self.observer, 'review.stopped', {'reason': 'unchanged_review_input',
                       'input_id': state.get('review_cache', {}).get('input_id')})
                return {'action': 'stop', 'stopped': True, 'stop_reason': 'unchanged_review_input'}
            action = state.get('review_action', '')
            if action and (state['verified'] or state.get('plan_revision_pending')):
                if (action == 'research' and (state['rounds'] >= 3 or state['reviews'] >= 3)
                        or action == 'revise' and state['reviews'] >= 3):
                    action = 'stop'
                if action == 'revise' and not state['evidence']:
                    action = 'stop'
                gaps = [f['message'] for f in state.get('review_findings', []) if f['blocking']]
                instruction = ('Address only the blocking review findings; retain supported facts. '
                               + ' '.join(gaps))[:2000]
                notify(self.observer, 'communication.parent_decision', {
                    'decision_source': 'review_action', 'action': action,
                    'stop_reason': 'review_action_stop' if action == 'stop' else ''})
                return {'action': action, 'instruction': instruction,
                        'research_goal': state['question'],
                        'queries': (state.get('queries') or gaps)[:3],
                        'dispatches': state['dispatches'] + 1,
                        'stopped': action == 'stop',
                        'stop_reason': 'review_action_stop' if action == 'stop' else ''}
        delivery_id = uuid4().hex
        results = state.get('subtask_results', [])
        result_budget = max(100, min(1000, (self.context_window - self.max_output_tokens) // 4))
        delivered = compact_results(results, result_budget) if self.tool_session is not None else []
        if self.tool_session is not None and self.observer is not None:
            notify(self.observer, 'communication.parent_delivery', {
                'delivery_id': delivery_id, 'source_results': results, 'delivered_results': delivered,
                'omitted_task_ids': [r['task_id'] for r in results
                                     if r['task_id'] not in {v['task_id'] for v in delivered}],
                'budget_estimated_tokens': result_budget,
                'source_estimated_tokens': estimate_tokens(json.dumps(results, ensure_ascii=False)),
                'delivered_estimated_tokens': estimate_tokens(json.dumps(delivered, ensure_ascii=False)),
                'compression_version': 'compact-results-v1',
            })
        with event_scope(parent_delivery_id=delivery_id):
            result = self.call('Supervisor',
            '动态拆解和维护任务清单。根据问题、已有证据和评审意见调度：缺证据 research；有证据未起草 draft；已有草稿的表述或引用问题 revise；核验充分通过 finish；无法继续 stop。instruction 写给下一角色的具体任务。'
            'research 时 research_goal 写本次子任务可独立理解的目标，依据历史消解指代；research_constraints 只写必要约束，不能复制完整消息历史。'
            '子任务 findings 和执行完成状态均未核验，不是最终答案；没有摘要也不代表没有已取回的证据。'
            '任务 done 不代表结论通过核验，不得绕过 Verifier。最多三轮检索、三轮草稿核验。'
            + (' Identify 1-8 facts or relations required by the original question, including intermediate '
               'bridges and qualifiers, as requirements. Do not invent their answers. Identify answer_type '
               '(boolean for yes/no). Preserve the frozen requirements on subsequent calls. '
               'Focus retrieval on uncovered requirements; input coverage is a fallible verifier report.' if policy else '')
            + (' Requirements describe what the user asks; do not put guessed entity types or answers in them. '
               'List uncertain entity types, references and interpretations in assumptions, which start '
               'unconfirmed. Only independent verification with quoted evidence may correct them. '
               'Asking about an entity location does not establish that entity is itself a city.'
               if policy and policy.revisable_assumptions else ''),
            RevisableDecision if policy and policy.revisable_assumptions else ControlledDecision if policy else Decision,
            {'question': state['question'], 'history': state['history'], 'tasks': state['tasks'],
                       'evidence': ([] if self.tool_session is not None else self.context(state)),
                       **({'subtask_results': delivered,
                           'available_evidence_count': len(state['evidence'])}
                          if self.tool_session is not None else {}),
                       'claims': state['claims'], 'supported': state['supported'],
                       'queries': state['queries'], 'issues': state['issues'], 'verified': state['verified'],
                       'missing_answerable': state.get('missing_answerable', []),
                       'research_rounds': state['rounds'], 'reviews': state['reviews'],
                       **({'requirements': state.get('requirements', []), 'coverage': state.get('coverage', [])}
                          if policy else {}),
                       **({'plan_assumptions': state.get('plan_assumptions', [])}
                          if policy and policy.revisable_assumptions else {})})
        action = result.action
        if policy and policy.revisable_assumptions:
            if not state.get('requirements'):
                notify(self.observer, 'plan.created', {'requirements': result.requirements,
                       'assumptions': [a.model_dump() for a in result.assumptions],
                       'assumption_status': 'unconfirmed'})
            elif result.requirements != state['requirements']:
                notify(self.observer, 'plan.requirement_change_rejected', {
                    'frozen': state['requirements'], 'proposed': result.requirements})
        if self.skills is not None and self.skills.automatic and action in {'research', 'draft', 'revise'}:
            self.skills.load(result.skill_ids)
            notify(self.observer, 'skills.loaded', {'skills': self.skills.used, 'warnings': self.skills.warnings})
        omissions = state.get('missing_answerable', [])
        instruction = result.instruction
        if state['rounds'] == 0 and (action in {'stop', 'finish'} or (state.get('input_evidence') and action in {'draft', 'revise'})):
            # An empty evidence set before retrieval is not a retrieval failure.
            # Require one search, then let normal evidence-based stopping apply.
            action = 'research'
            instruction = '先围绕用户原问题检索本地文献，不得在尚未检索时判定证据不足。'
        if state['verified'] and omissions:
            # Existing evidence suffices: do not let an early finish/stop discard
            # an identified question item. The normal review limit still applies.
            action = 'revise' if state['claims'] else 'draft'
            instruction = '保留已有正确结论，依据给定证据补全 missing_answerable 中的每个问题项，再交核验。'
        if state.get('citation_errors') and state['reviews'] < 3:
            action = 'revise'
            instruction = '只修订越界证据引用，依据 review_issues 中的有效编号重新选择支持证据；不能将 PDF 元素 ID 当作引用。无法支持的结论应删除，不要为编号错误重复检索。'
        complete = (state['verified'] and bool(state['claims'])
                    and len(state['supported']) == len(state['claims'])
                    and not state['queries'] and not state['issues'] and not omissions
                    and not state.get('citation_errors')
                    and (not policy or state.get('coverage_complete')))
        requested_action = action
        # A clean verdict is terminal; another search would invalidate its support state.
        if complete:
            action = 'finish'
        elif policy and action in {'finish', 'stop'} and state['evidence'] and not state['verified'] and state['reviews'] < 3:
            action = 'draft'
        elif action == 'revise' and not state['claims'] and state['evidence'] and state['reviews'] < 3:
            action = 'draft'
        if requested_action != action:
            notify(self.observer, 'rag.dispatch_guard', {'requested_action': requested_action,
                   'action': action, 'reason': 'verified_complete' if complete else 'no_draft_yet'})
        invalid = ((action == 'research' and state['rounds'] >= 3)
                   or (action in {'draft', 'revise'} and (not state['evidence'] or state['reviews'] >= 3))
                   or (action == 'revise' and not state['claims']))
        if invalid:
            if action == 'research':
                stop_reason = 'research_limit'
            elif not state['evidence']:
                stop_reason = 'no_evidence_for_draft'
            elif state['reviews'] >= 3:
                stop_reason = 'review_limit'
            else:
                stop_reason = 'revise_without_draft'
            action = 'stop'
        else:
            stop_reason = 'supervisor_stop' if action == 'stop' else ''
        parent_call_id = self.last_call_ids.get('Supervisor')
        notify(self.observer, 'communication.parent_decision', {
            'delivery_id': delivery_id, 'call_id': parent_call_id, 'decision_source': 'model_and_guards',
            'model_action': result.action, 'action': action, 'instruction': instruction,
            'stop_reason': stop_reason, 'verified_before_decision': state['verified'],
            'task_ids': [r['task_id'] for r in results],
        })
        return {'action': action, 'tasks': [t.model_dump() for t in result.tasks],
                **({'plan_assumptions': (state.get('plan_assumptions', []) if state.get('requirements')
                    else [{**a.model_dump(), 'status': 'unconfirmed', 'citations': [], 'quote': ''}
                          for a in result.assumptions])}
                   if policy and policy.revisable_assumptions else {}),
                **({'requirements': state.get('requirements') or result.requirements,
                    'answer_type': state.get('answer_type', 'other') if state.get('requirements') else result.answer_type}
                   if policy else {}),
                'parent_call_id': parent_call_id,
                'research_goal': result.research_goal, 'research_constraints': result.research_constraints,
                'instruction': instruction, 'dispatches': state['dispatches'] + 1,
                'stop_reason': stop_reason,
                'stopped': action == 'stop' or (action == 'finish' and not complete)}


