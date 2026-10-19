"""Compatibility bridge; the policy implementation lives in search_policy."""
from search_policy.intent import SearchIntent as _SearchIntent, PolicyServiceError
from deepsearch_agent.core.models import ModelServiceError


class SearchIntent(_SearchIntent):
    def __init__(self, client, **kwargs):
        kwargs.setdefault('service_errors', (PolicyServiceError, ModelServiceError))
        super().__init__(client, **kwargs)
