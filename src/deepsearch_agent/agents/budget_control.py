"""Optional, label-free resource controls; never decide evidential support."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class BudgetPolicy:
    finalization_calls: int = 5
    no_progress_searches: int = 2
    finalization_seconds: float = 30.0

    def __post_init__(self):
        for value in (self.finalization_calls, self.no_progress_searches):
            if type(value) is not int or value < 1:
                raise ValueError('Budget policy counts must be positive integers')
        if (type(self.finalization_seconds) not in (int, float)
                or not math.isfinite(self.finalization_seconds) or self.finalization_seconds < 0):
            raise ValueError('Budget time reserve must be finite and nonnegative')


class ResearchBudgetControl:
    def __init__(self, snapshot, *, bounded=False, policy=None):
        self.read_snapshot = snapshot
        self.bounded = bounded
        self.policy = policy or BudgetPolicy()
        self.seen_chunks = set()
        self.stalled_searches = 0
        self.stop_reason = ''

    def observe_search(self, chunk_ids):
        new = set(chunk_ids) - self.seen_chunks
        self.stalled_searches = 0 if new else self.stalled_searches + 1
        self.seen_chunks.update(chunk_ids)

    def snapshot(self):
        raw = self.read_snapshot()
        keys = ('model_calls_remaining', 'search_calls_remaining', 'tokens_remaining',
                'seconds_remaining', 'max_output_tokens')
        values = {}
        for key in keys:
            value = raw[key]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('Invalid budget counter')
            values[key] = max(0, value)
        return {**values, 'policy': asdict(self.policy),
                'tokens_remaining_are_accounted_not_exact': True,
                'consecutive_searches_without_new_admitted_chunks': self.stalled_searches,
                'new_chunk_count_is_not_evidence_sufficiency': True}

    def research_stop(self):
        if not self.bounded:
            return ''
        if self.stop_reason:
            return self.stop_reason
        s = self.snapshot()
        conditions = (
            (s['search_calls_remaining'] <= 0, 'search_budget_reserved'),
            (s['model_calls_remaining'] <= self.policy.finalization_calls, 'model_calls_reserved'),
            (s['tokens_remaining'] <= self.policy.finalization_calls * s['max_output_tokens'], 'token_reserve_heuristic'),
            (s['seconds_remaining'] <= self.policy.finalization_seconds, 'time_reserve_heuristic'),
            (self.stalled_searches >= self.policy.no_progress_searches, 'no_new_admitted_chunks'),
        )
        self.stop_reason = next((reason for hit, reason in conditions if hit), '')
        return self.stop_reason
