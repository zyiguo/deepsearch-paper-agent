from dataclasses import dataclass

class ModelServiceError(RuntimeError):
    CODES = frozenset({'model_service_error', 'timeout', 'connection_error',
                       'authentication_error', 'permission_denied', 'rate_limit',
                       'server_error', 'request_rejected', 'invalid_json',
                       'invalid_response', 'empty_response', 'output_truncated', 'content_filtered'})

    def __init__(self, message: str, status_code: int = 502, *,
                 error_code: str = 'model_service_error', provider_status_code: int | None = None,
                 usage: dict | None = None, finish_reason: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code if error_code in self.CODES else 'model_service_error'
        self.provider_status_code = (provider_status_code if type(provider_status_code) is int
                                     and 100 <= provider_status_code <= 599 else None)
        self.retryable = self.error_code in {'timeout', 'connection_error', 'rate_limit', 'server_error'}
        self.finish_reason = finish_reason if finish_reason in {'stop', 'length', 'content_filter', 'tool_calls'} else None
        usage = usage if isinstance(usage, dict) else {}
        self.prompt_tokens = optional_count(usage.get('prompt_tokens'))
        self.completion_tokens = optional_count(usage.get('completion_tokens'))
        self.total_tokens = optional_count(usage.get('total_tokens'))

    def diagnostics(self) -> dict:
        # Never serialize the exception message, provider body, headers, or URL.
        return {'error_code': self.error_code, 'provider_status_code': self.provider_status_code,
                'retryable': self.retryable, 'finish_reason': self.finish_reason,
                'prompt_tokens': self.prompt_tokens, 'completion_tokens': self.completion_tokens,
                'reported_total_tokens': self.total_tokens}


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


def optional_count(value):
    # bool 是 int 的子类，这里必须排除。
    return value if type(value) is int and value >= 0 else None
