"""Bounded local-document research workflow; no tracing or checkpoint storage."""


from deepsearch_tracing.context import notify, event_scope
from deepsearch_agent.core.identity import fingerprint


class ResearcherMixin:
    def research(self, state):
        if self.tool_session is not None:
            from deepsearch_agent.agents.agentic_research import AgenticResearcher
            from deepsearch_agent.agents.subagent_contracts import SubagentTask
            assumptions = []
            if self.evidence_policy and self.evidence_policy.revisable_assumptions:
                assumptions = [('Provisional interpretations, not evidence: ' + '; '.join(
                    a['key'] + '=' + a['statement'] + ' (' + a['status'] + ')'
                    for a in state.get('plan_assumptions', [])))[:1000]]
            execution = getattr(self.tool_session, 'execution', None)
            snapshot = state.get('tool_session_state')
            if execution is not None and snapshot is not None:
                self.tool_session.restore_runtime(snapshot)
            task_identity = ({'task_id': fingerprint(['research-child-v1', self.run_id, state['rounds']])[:32]}
                             if execution is not None else {})
            task = SubagentTask(**task_identity, goal=state.get('research_goal') or state['question'], instruction=state['instruction'],
                gaps=[q[:1000] for q in state.get('queries', [])[:3] if q.strip()],
                constraints=['Use only the authorized local document scope.',
                             'Return candidate findings with original evidence IDs; do not claim verification.',
                             'Resolve references using the explicit goal and task instruction; '
                             'report ambiguity instead of guessing absent conversation history.',
                             *assumptions[:1],
                             *state.get('research_constraints', [])[:4 if assumptions else 5]],
                top_k=state['top_k'])
            previous_ids = {c.chunk_id for c in self.tool_session.evidence}
            initial_calls = self.tool_session.calls
            def stop_research():
                reason = self.budget_control.research_stop() if self.budget_control else ''
                if reason:
                    return reason
                if self.evidence_policy and self.tool_session.calls - initial_calls >= self.evidence_policy.child_tool_calls:
                    return 'coverage_probe_due'
                return ''
            external = None
            external = self.external_tools
            def plan_tool(instruction, schema, data):
                visual, seen = [], set()
                for chunk in [*state.get('input_evidence', []),
                              *(c for c in self.tool_session.evidence if c.chunk_id not in previous_ids)]:
                    pages = [p for p in chunk.visual_pages if (chunk.source_path, p) not in seen]
                    if pages:
                        seen.update((chunk.source_path, p) for p in pages)
                        visual.append({'source': chunk.source_path, 'visual_pages': pages,
                                       'chunk_id': chunk.chunk_id})
                if visual:
                    data = {**data, 'evidence': visual}
                return self.call('Researcher', instruction, schema, data, isolated=True)
            researcher = AgenticResearcher(self.tool_session, tools_factory=self.tools_factory,
                planner=plan_tool,
                max_steps=self.tool_session.settings.max_steps_per_round, observer=self.observer,
                external_tools=external,
                operation_scope=f'research:{state["rounds"]}',
                stop_check=(stop_research if self.budget_control or self.evidence_policy else None),
                context_budget_tokens=max(600, (self.context_window - self.max_output_tokens) // 2))
            with event_scope(task_id=task.task_id, parent_call_id=state.get('parent_call_id'),
                             research_round=state['rounds'], research_run_id=self.run_id):
                if self.observer is not None:
                    notify(self.observer, 'communication.task_dispatched', {
                        'communication_trace_version': 1, 'task': task.model_dump(),
                        'parent_context': {
                            'question': state['question'], 'history': state['history'],
                            'tasks': state['tasks'], 'instruction': state['instruction'],
                            'gaps': state.get('queries', []), 'issues': state.get('issues', []),
                            'memory_context': self.memory_context,
                            'existing_evidence_ids': [c.chunk_id for c in state['evidence']],
                            **({'plan_assumptions': state.get('plan_assumptions', [])}
                               if self.evidence_policy and self.evidence_policy.revisable_assumptions else {}),
                        },
                    })
                result = researcher.run_task(task, state['evidence'])
            # Resolve IDs from the authoritative scoped service, never model-produced text.
            existing_ids = {c.chunk_id for c in state['evidence']}
            gathered = [c for c in self.tool_session.evidence
                        if c.chunk_id in result.evidence_ids and c.chunk_id not in existing_ids]
            notify(self.observer, 'research.tools_done', {'round': state['rounds'],
                   'stop_reason': researcher.stop_reason, 'remaining_calls': self.tool_session.remaining_calls})
            return {'evidence': [*state['evidence'], *gathered], 'rounds': state['rounds'] + 1,
                    **({'tool_session_state': self.tool_session.runtime_snapshot()} if execution is not None else {}),
                    'subtask_results': [*state.get('subtask_results', []), result.model_dump()][-3:],
                    'supported': [], 'verified': False,
                    **({'review_action': '', 'review_stalled': False, 'plan_revision_pending': False}
                       if self.evidence_policy and self.evidence_policy.version == 'evidence-controlled-v2' else {}),
                    **({'coverage': [], 'coverage_complete': False} if self.evidence_policy else {})}
        raise RuntimeError('Production research requires a scoped tool session')
