"""Paper-informed inference rules; no gold labels, learned models or storage."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class EvidencePolicy:
    version: str = 'evidence-controlled-v1'
    child_tool_calls: int = 2
    repairs_per_role: int = 1
    structured_reviews: bool = False
    revisable_assumptions: bool = False
    multi_candidates: bool = False
    hard_pruning: bool = False

    @property
    def candidate_reuse(self):
        return self.version in {'evidence-controlled-v4', 'evidence-controlled-v5'}

    @property
    def incremental_gaps(self):
        return self.version == 'evidence-controlled-v5'

    @classmethod
    def v5(cls):
        return cls(version='evidence-controlled-v5', structured_reviews=True,
                   multi_candidates=True, hard_pruning=True)

    @classmethod
    def v4(cls):
        return cls(version='evidence-controlled-v4', structured_reviews=True,
                   multi_candidates=True, hard_pruning=True)

    @classmethod
    def v3(cls, *, hard_pruning=True):
        return cls(version='evidence-controlled-v3', structured_reviews=True,
                   multi_candidates=True, hard_pruning=hard_pruning)

    @classmethod
    def v2(cls, *, structured_reviews=True, revisable_assumptions=True):
        return cls(version='evidence-controlled-v2', structured_reviews=structured_reviews,
                   revisable_assumptions=revisable_assumptions)

    def __post_init__(self):
        if self.version not in {'evidence-controlled-v1', 'evidence-controlled-v2', 'evidence-controlled-v3', 'evidence-controlled-v4', 'evidence-controlled-v5'}:
            raise ValueError('Unknown evidence policy version')
        if any(type(v) is not bool for v in (self.structured_reviews, self.revisable_assumptions,
                                            self.multi_candidates, self.hard_pruning)):
            raise ValueError('Invalid evidence policy flags')
        if (self.multi_candidates or self.hard_pruning) and self.version not in {'evidence-controlled-v3', 'evidence-controlled-v4', 'evidence-controlled-v5'}:
            raise ValueError('Candidate paths require a v3 identity')
        if self.version in {'evidence-controlled-v3', 'evidence-controlled-v4', 'evidence-controlled-v5'} and (not self.multi_candidates or not self.structured_reviews
                                                        or self.revisable_assumptions):
            raise ValueError('V3 uses independent path reviews and frozen requirements')
        if self.version == 'evidence-controlled-v1' and (self.structured_reviews or self.revisable_assumptions):
            raise ValueError('V2 features require a v2 identity')
        if type(self.child_tool_calls) is not int or not 1 <= self.child_tool_calls <= 18:
            raise ValueError('Invalid child probe cadence')
        if self.repairs_per_role != 1 or type(self.repairs_per_role) is not int:
            raise ValueError('One strict repair per role is supported')


def coverage_result(requirements, coverage, valid_ids):
    """Model-reported coverage is accepted only with unambiguous authorized IDs."""
    by_requirement = {}
    for item in coverage:
        index = item['requirement_index']
        by_requirement.setdefault(index, []).append(item)
    gaps = []
    for index, requirement in enumerate(requirements):
        items = by_requirement.get(index, [])
        covered = (len(items) == 1 and items[0]['status'] == 'supported'
                   and bool(items[0]['citations']) and set(items[0]['citations']) <= valid_ids)
        if not covered:
            gaps.append(requirement)
    if any(type(i) is not int or not 0 <= i < len(requirements) for i in by_requirement):
        return list(requirements), False
    return gaps, bool(requirements) and not gaps


class SessionResearchControl:
    """Production tool/time boundary; unknown token/model budgets stay unknown."""
    def __init__(self, session):
        self.session = session
        self.stop_reason = ''

    def snapshot(self):
        return {'tool_calls_remaining': self.session.remaining_calls,
                'seconds_remaining': max(0, self.session.deadline - time.monotonic()),
                'model_calls_remaining': None, 'tokens_remaining': None,
                'unknown_budgets_are_not_zero': True}

    def research_stop(self):
        if not self.stop_reason:
            snapshot = self.snapshot()
            if snapshot['tool_calls_remaining'] <= 0:
                self.stop_reason = 'session_tool_budget'
            elif snapshot['seconds_remaining'] <= min(30, self.session.settings.timeout_seconds / 5):
                self.stop_reason = 'session_time_reserve'
        return self.stop_reason
