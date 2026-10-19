"""Bounded local-document research workflow; no tracing or checkpoint storage."""
import json
from dataclasses import asdict


from deepsearch_tracing.context import notify


from deepsearch_agent.agents.contracts import Draft, Verdict, CoverageVerdict, ActionableVerdict
from deepsearch_agent.agents.evidence_policy import coverage_result
from deepsearch_agent.agents.review_control import review_input_id, classify_findings, apply_assumption_updates

class AnsweringMixin:
    def draft(self, state):
        if self.evidence_policy and self.evidence_policy.multi_candidates:
            return self.draft_paths(state)
        result = self.call('Answerer', '先按原问题逐项检查所问属性，对每个有证据的属性分别写一个 claim，不得只回答其中部分。修订时保留正确结论并优先补全 missing_answerable。每个 claim 只含一个可验证结论，citations 只能取当前 evidence 对象的整数 id，不是页码、PDF 元素 ID 或 chunk_id。用中文回答，text 不自行插入引用编号。无法支持的内容不写；没有证据可回答则 claims 为空。', Draft,
                           {'question': state['question'], 'history': state['history'], 'evidence': self.context(state),
                            'tasks': state['tasks'], 'instruction': state['instruction'],
                            'previous_draft': state['claims'], 'review_issues': state['issues'],
                            'missing_answerable': state.get('missing_answerable', []),
                            **({'requirements': state.get('requirements', []), 'answer_type': state.get('answer_type', 'other')}
                               if self.evidence_policy else {}),
                            **({'review_findings': state.get('review_findings', []),
                                'plan_assumptions': state.get('plan_assumptions', [])}
                               if self.evidence_policy and self.evidence_policy.structured_reviews else {})})
        return {'claims': [c.model_dump() for c in result.claims], 'supported': [], 'verified': False,
                'citation_errors': [],
                **({'review_stalled': False, 'plan_revision_pending': False}
                   if self.evidence_policy and self.evidence_policy.version == 'evidence-controlled-v2' else {}),
                **({'coverage': [], 'coverage_complete': False} if self.evidence_policy else {})}


    def verify(self, state):
        if self.evidence_policy and self.evidence_policy.multi_candidates:
            return self.verify_paths(state)
        evidence = self.context(state)
        valid_ids = {item['id'] for item in evidence}
        policy = self.evidence_policy
        actionable = policy and (policy.structured_reviews or policy.revisable_assumptions)
        cache_key = review_input_id(state['question'], state['claims'], evidence,
            state.get('requirements', []), state.get('plan_assumptions', []),
            asdict(policy), context={'memory_context': self.memory_context,
                'model': {key: getattr(getattr(self.client, 'settings', None), key, None)
                          for key in ('model', 'base_url', 'temperature', 'max_output_tokens')}}) if actionable else ''
        cached = state.get('review_cache', {})
        visual = any(item.get('visual_pages') for item in evidence)
        if policy and policy.structured_reviews and not visual and cached.get('input_id') == cache_key:
            notify(self.observer, 'verification.reused', {'input_id': cache_key,
                   'reason': 'identical_verifier_input', 'no_new_model_call': True})
            return {**cached['result'], 'reviews': state['reviews'], 'review_cache': cached,
                    'review_stalled': True}
        citation_errors = [{'claim': i, 'invalid': sorted(set(claim['citations']) - valid_ids)}
                           for i, claim in enumerate(state['claims']) if not set(claim['citations']) <= valid_ids]
        if citation_errors:
            notify(self.observer, 'rag.citation_repair', {'errors': citation_errors, 'valid_ids': sorted(valid_ids)})
            result = {'supported': [], 'queries': [], 'missing_answerable': [], 'verified': True,
                    'reviews': state['reviews'] + 1, 'citation_errors': citation_errors,
                    **({'coverage': [], 'coverage_complete': False} if self.evidence_policy else {}),
                    **({'review_action': 'revise', 'review_stalled': False,
                        'review_findings': [], 'review_notes': []} if actionable else {}),
                    'issues': ['Invalid evidence citations: ' + json.dumps(citation_errors) +
                               '; valid evidence IDs: ' + json.dumps(sorted(valid_ids))]}
            if actionable:
                result['review_cache'] = {'input_id': cache_key, 'result': dict(result)}
            return result
        verdict = self.call('Verifier', '分两步独立核查。第一步检查真实性：仅当引用证据直接支持完整结论且无明显矛盾时，才将其零起始索引放入 supported；不要仅因有引用就通过。第二步检查完整性：从原问题逐项列出全部所问属性，与第一步通过的结论逐项对照，不能只检查草稿里已经写出的内容。有证据支持但通过的结论中未回答的属性，必须逐项放入 missing_answerable，包含 question 和支持它的证据 citations（从1开始）；没有漏项时显式返回空列表。被误写或未通过核验的属性如果证据充分也应列入以便修订。仅在现有证据确实不足时才生成 queries 补查，不要为已有证据的漏答重复检索。' + (' Independently check every frozen requirement, including bridge relations and qualifiers, against the actual evidence, not child summaries. Return exactly one coverage item per zero-based requirement_index. A supported item needs valid one-based evidence citations; missing or partial relations must stay unresolved. Check the question itself for requirements omitted by the plan. Do not turn budget exhaustion into support.' if self.evidence_policy else '') + (
            ' Return typed findings: missing_evidence -> research, contradiction/citation_error -> revise, '
            'evidence_conflict -> research or stop, and nonblocking note -> finish. Each finding identifies '
            'affected zero-based requirement_indices and claim_indices. A real ambiguity changing the answer '
            'must block; scope notes that do not affect it may be notes. Leave legacy issues empty only '
            'when every issue is represented in findings. Never silently relabel a conflict as a note. '
            'Assumptions are provisional interpretations, not facts. Propose assumption_updates only when '
            'authorized evidence supports or rejects them, with exact quotes and one-based citations. '
            'Do not modify the frozen user requirements or invent missing bridges.' if actionable else ''),
            ActionableVerdict if actionable else CoverageVerdict if self.evidence_policy else Verdict,
                            {'question': state['question'], 'claims': state['claims'], 'evidence': evidence,
                             **({'requirements': state.get('requirements', [])} if self.evidence_policy else {}),
                             **({'plan_assumptions': state.get('plan_assumptions', [])} if actionable else {})})
        supported = [i for i in sorted(set(verdict.supported)) if 0 <= i < len(state['claims'])
                     and all(ref in valid_ids for ref in state['claims'][i]['citations'])]
        issues = list(verdict.issues)
        review = {}
        if actionable:
            findings = [f.model_dump() for f in verdict.findings]
            if policy.structured_reviews:
                issues, notes, action = classify_findings(findings, issues,
                    len(state.get('requirements', [])), len(state['claims']))
            else:
                issues += [f['message'] for f in findings if f['blocking']]
                notes, action = [], ''
            review = {'review_findings': findings, 'review_notes': notes,
                      'review_action': action, 'review_stalled': False}
        if len(supported) < len(state['claims']):
            issues.append('部分结论未通过支持关系或引用编号校验。')
        omissions = []
        for item in verdict.missing_answerable:
            if item.question.strip() and all(ref in valid_ids for ref in item.citations):
                omissions.append(item.model_dump())
            else:
                issues.append('漏项的支持证据编号无效，无法确认该项可回答。')
        coverage = {}
        if self.evidence_policy:
            ledger = [item.model_dump() for item in verdict.coverage]
            gaps, complete = coverage_result(state.get('requirements', []), ledger, valid_ids)
            if gaps:
                issues.extend('Uncovered requirement: ' + gap for gap in gaps)
            coverage = {'coverage': ledger, 'coverage_complete': complete}
            notify(self.observer, 'verification.coverage', {**coverage, 'gaps': gaps,
                   'coverage_is_model_report_not_ground_truth': True})
        if actionable:
            if not complete and review['review_action'] not in {'stop', 'research'}:
                review['review_action'] = 'research'
            elif issues and not review['review_action']:
                review['review_action'] = 'revise'
            if verdict.queries and review['review_action'] not in {'stop', 'research'}:
                review['review_action'] = 'research'
            elif omissions and not review['review_action']:
                review['review_action'] = 'revise'
            if policy.revisable_assumptions:
                assumptions, changes, rejected = apply_assumption_updates(
                    state.get('plan_assumptions', []), [u.model_dump() for u in verdict.assumption_updates], evidence)
                if rejected:
                    issues.append('Unproven assumption updates: ' + ', '.join(rejected))
                    review['review_action'] = 'stop'
                if changes:
                    notify(self.observer, 'plan.assumptions_updated', {'changes': changes,
                           'provenance_check_only': True, 'semantic_judge': 'independent_verifier'})
                    review.update(plan_assumptions=assumptions, plan_revision_pending=True,
                                  review_action='research')
                    coverage['coverage_complete'] = False
                    supported = []
        result = {'supported': supported, 'queries': [q.strip()[:1000] for q in verdict.queries if q.strip()],
                'issues': issues, 'missing_answerable': omissions,
                'verified': not review.get('plan_revision_pending', False), 'reviews': state['reviews'] + 1,
                  'citation_errors': [], **coverage, **review}
        if actionable:
            result['review_cache'] = {'input_id': cache_key, 'result': dict(result)}
        return result
