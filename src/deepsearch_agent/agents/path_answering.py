"""Batch candidates share retrieval and independent verification, with local proof checks."""
from deepsearch_tracing.context import notify
from uuid import uuid4
from deepsearch_agent.core.evidence_paths import inspect_candidates, resolve_candidates, merge_candidate_pool
from .path_contracts import CandidateDraft, CandidateVerdict, CandidateUpdates, GapVerdict
from deepsearch_agent.core.verification_gaps import record_gaps, filter_review_paths
from deepsearch_agent.core.candidate_reuse import registry, merge_updates
from deepsearch_agent.core.evidence_paths import fingerprint


class PathAnsweringMixin:
    def gap_scope(self, state):
        if not hasattr(self, '_candidate_reuse_session'):
            self._candidate_reuse_session = uuid4().hex
        return fingerprint({'question': state['question'], 'requirements': state.get('requirements', []),
            'instruction': state.get('instruction', ''),
            'answer_type': state.get('answer_type'), 'memory': getattr(self, 'memory_context', None),
            'notes': self.working_notes(), 'skills': self.skills.prompt() if self.skills else '',
            'client': id(self.client), 'session': self._candidate_reuse_session,
            'policy': self.evidence_policy.version})

    def draft_paths(self, state):
        evidence = self.context(state)
        reuse = self.evidence_policy.candidate_reuse
        if reuse and not hasattr(self, '_candidate_reuse_session'):
            self._candidate_reuse_session = uuid4().hex
        generation_key = fingerprint({'question': state['question'],
            'requirements': state.get('requirements', []), 'evidence': evidence,
            'instruction': state['instruction'], 'answer_type': state.get('answer_type'),
            'memory': getattr(self, 'memory_context', None), 'notes': self.working_notes(),
            'skills': self.skills.prompt() if self.skills else '',
            'session': self._candidate_reuse_session, 'client_instance': id(self.client),
            'policy': self.evidence_policy.version}) if reuse else ''
        if (reuse and generation_key == state.get('candidate_generation_key')
                and state.get('candidates') and not state.get('coverage_complete')):
            notify(self.observer, 'candidates.generation_skipped',
                   {'reason': 'unchanged_evidence_and_task', 'input_id': generation_key})
            return {'candidate_generation_reused': True, 'verified': False}
        result = self.call('Answerer',
            'Generate up to four DISTINCT plausible answer/interpretation candidates, only when relevant. '
            'Do not pad to four. Each has up to three ALTERNATIVE evidence paths; each path must establish '
            'ALL frozen requirements (AND), while alternative paths are OR. Each step states a bridge '
            'claim, its zero-based requirement_index, a one-based authorized citation and an EXACT '
            'verbatim quote from that evidence text. A quote proves provenance, not entailment. '
            'Keep candidates lacking evidence with empty paths/steps/quotes; do not fabricate quotes. '
            'There are four candidate slots for the entire run: reuse the exact answer/interpretation '
            'of an existing candidate when adding evidence; unresolved candidates cannot be evicted. '
            'Separate homonyms, locations, time and role interpretations. Preserve unresolved prior '
            'candidates when still plausible. A candidate answer is the complete answer to the question, '
            'not an intermediate entity. Use the requested language. Never use external knowledge.'
            + (' For an existing proposition set existing_candidate_id to its supplied ID. '
               'Its answer and interpretation are immutable: add paths, never paraphrase it into a new slot. '
               'Use null only for a genuinely different proposition. Keep answers concise.' if reuse else '')
            + (' Prior verification_gaps are fallible missing-requirement reports. Supply NEW quoted '
               'witnesses that address them; do not turn statements that evidence is missing into '
               'supporting proof steps. Retain unknown candidates with empty paths when no bridge '
               'can be added. A new interpretation is a new candidate, not an edit of an old identity.'
               if self.evidence_policy.incremental_gaps else ''),
            CandidateUpdates if reuse else CandidateDraft, {'question': state['question'], 'requirements': state.get('requirements', []),
                'answer_type': state.get('answer_type'), 'evidence': evidence,
                'previous_candidates': registry(state.get('candidates', [])) if reuse else state.get('candidates', []), 'issues': state.get('issues', []),
                'instruction': state['instruction'],
                **({'verification_gaps': {k: {'missing_requirements': g['missing_requirements']}
                    for k, g in state.get('verification_gaps', {}).items()}}
                   if self.evidence_policy.incremental_gaps and
                   state.get('verification_gap_scope') == self.gap_scope(state) else {})})
        proposals = [c.model_dump() for c in result.candidates]
        pool, overflow = (merge_updates if reuse else merge_candidate_pool)(state.get('candidates', []), proposals)
        records = inspect_candidates(pool, evidence, hard_pruning=self.evidence_policy.hard_pruning)
        snapshot = {'round': state['reviews'], 'evidence': evidence, 'candidates': records,
                    'overflow_proposals': overflow, 'overflow_reason': 'fixed_candidate_capacity' if overflow else ''}
        if reuse:
            snapshot.update(raw_proposals=proposals, rejected_updates=overflow)
        notify(self.observer, 'candidates.proposed', snapshot)
        # Bounded by the existing three-review guard; old snapshots are never deleted.
        return {'candidates': records, 'candidate_evidence': evidence,
                'candidate_generation_key': generation_key, 'candidate_generation_reused': False,
                'candidate_history': state.get('candidate_history', []) + [snapshot],
                'claims': [], 'supported': [], 'verified': False,
                'coverage': [], 'coverage_complete': False, 'citation_errors': [],
                'review_stalled': False, 'plan_revision_pending': False}

    def verify_paths(self, state):
        # Recompute against the *same actual prompt* used for generation, so truncation
        # changes cannot falsely invalidate an otherwise valid quote.
        evidence = state['candidate_evidence']
        records = state['candidates']
        payload = []
        for ci, candidate in enumerate(records):
            payload.append({'candidate_index': ci, 'answer': candidate['answer'],
                'interpretation': candidate['interpretation'],
                'paths': [{'path_index': pi, 'steps': path['steps']}
                          for pi, path in enumerate(candidate['paths']) if not path['pruned']]})
        gap_mode = self.evidence_policy.incremental_gaps
        scope = self.gap_scope(state) if gap_mode else ''
        gaps = state.get('verification_gaps', {}) if state.get('verification_gap_scope') == scope else {}
        deferred = []
        if gap_mode:
            payload, deferred = filter_review_paths(payload, records, evidence, gaps)
            if deferred:
                notify(self.observer, 'candidates.gap_review_deferred', {
                    'paths': deferred, 'scope': scope, 'evidence_id': fingerprint(evidence),
                    'semantic_review_performed': False, 'candidate_deletion': False})
        skipped = self.evidence_policy.candidate_reuse and (
            state.get('candidate_generation_reused') or
            not any(p['steps'] for c in payload for p in c['paths']))
        if skipped:
            stalled = bool(state.get('candidate_generation_reused'))
            verdict = CandidateVerdict(paths=[], selected_candidate=None, answer_resolved=False,
                issues=['Evidence remains insufficient; candidates are retained as unknown.'],
                queries=[] if stalled else ((state.get('queries') if gap_mode else None) or
                    [r[:600] for r in state.get('requirements', [])[:3]]))
            notify(self.observer, 'candidates.verification_skipped', {
                'reason': 'unchanged_unknown_input' if stalled else
                          'confirmed_gap_without_new_witness' if deferred else 'no_nonempty_proof',
                'input_id': state.get('candidate_generation_key'), 'semantic_review_performed': False})
        else:
            verdict = self.call('Verifier',
            'Independently verify each submitted candidate/path against ACTUAL evidence and the '
            'original question. Treat every path as AND of all requirements, alternatives as OR. '
            'Check role, entity identity, geography, dates, units and every bridge; identical names '
            'do not establish identity. Quotes existing in a source do not imply a claim is true. '
            'Return one review per provided path, retaining its explicit candidate_index/path_index. '
            'Missing evidence is unknown; contradictions are semantic judgments, never deletion '
            'instructions. Do not review omitted paths. Select a candidate ONLY when a full path '
            'supports its complete answer and material ambiguity is resolved. Different supported '
            'answers require abstention. Uncertain candidates stay available for further research. '
            'answer_resolved requires no blocking issues or queries. Provide up to three discriminating '
            'queries to resolve unknown bridges; do not request evidence already present.'
            + (' Be concise: reason <= 280 characters per path, preferably one sentence. '
               'At most 3 short issues and 3 short queries, each <= 180 characters. '
               'Do not restate quotations or the question. supported_requirements must list only '
               'facts actually established, not an explanation of their absence. '
               'Deferred paths remain unknown; never report them as supported.' if gap_mode else ''),
            GapVerdict if gap_mode else CandidateVerdict,
            {'question': state['question'], 'requirements': state.get('requirements', []),
             'candidates': payload, 'evidence': evidence,
             **({'deferred_unknown_paths': deferred} if gap_mode else {})})
        records, selected = resolve_candidates(records, verdict.model_dump(), len(state.get('requirements', [])))
        claims, ledger = [], []
        if selected is not None:
            path = next(p for p in records[selected]['paths'] if p['status'] == 'supported')
            citations = sorted({s['citation'] for s in path['steps']})
            claims = [{'text': records[selected]['answer'], 'citations': citations}]
            ledger = [{'requirement_index': i, 'status': 'supported',
                       'citations': sorted({s['citation'] for s in path['steps'] if s['requirement_index'] == i})}
                      for i in range(len(state['requirements']))]
        snapshot = {'round': state['reviews'], 'candidates': records,
                    'selected_candidate': selected, 'review': verdict.model_dump(),
                    'hard_pruning': self.evidence_policy.hard_pruning,
                    'semantic_support_is_model_report': True, 'semantic_review_performed': not skipped}
        notify(self.observer, 'candidates.reviewed', snapshot)
        complete = selected is not None
        if gap_mode:
            # Only real independent reviews add gap reports. Deferred and empty
            # paths preserve earlier unknown gaps; they cannot manufacture support.
            if not skipped:
                for item in payload:
                    if item['paths']:
                        gaps = {k: v for k, v in gaps.items()
                                if k != records[item['candidate_index']]['candidate_id']}
                gaps = {**gaps, **record_gaps(records, verdict.model_dump(), evidence,
                                            len(state.get('requirements', [])))}
            notify(self.observer, 'candidates.gap_state', {'scope': scope, 'gaps': gaps})
        return {'candidates': records, 'selected_candidate': selected,
                **({'verification_gaps': gaps, 'verification_gap_scope': scope} if gap_mode else {}),
                'claims': claims, 'supported': list(range(len(claims))), 'verified': True,
                'reviews': state['reviews'] + 1, 'coverage': ledger, 'coverage_complete': complete,
                'queries': verdict.queries, 'issues': [] if complete else
                verdict.issues or ['No unambiguous complete candidate path has been verified.'],
                'missing_answerable': [], 'citation_errors': [],
                'review_action': '' if complete else 'research' if verdict.queries else 'stop',
                'review_findings': [], 'review_notes': [],
                'review_stalled': bool(skipped and state.get('candidate_generation_reused'))}
