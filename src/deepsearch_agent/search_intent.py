"""Compatibility bridge; the policy implementation lives in search_policy."""
from search_policy.intent import (SYSTEM, SearchDecision, parse_decision,
                                  SearchIntent as _SearchIntent, PolicyServiceError)
from deepsearch_agent.llm_gateway import ModelServiceError


class SearchIntent(_SearchIntent):
    def __init__(self, client, **kwargs):
        kwargs.setdefault('service_errors', (PolicyServiceError, ModelServiceError))
        super().__init__(client, **kwargs)
