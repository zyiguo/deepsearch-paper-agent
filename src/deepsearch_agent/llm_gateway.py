from dataclasses import dataclass

import httpx

from deepsearch_agent.llm_config import LLMSettings


class ModelServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


def optional_text(value):
    return value if isinstance(value, str) and value else None


def optional_count(value):
    # bool 是 int 的子类，这里必须排除。
    return value if type(value) is int and value >= 0 else None


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        if settings.backend != "openai_compatible":
            raise ValueError("真实客户端需要 openai_compatible 配置")

        self.settings = settings
        self._http = httpx.Client(
            timeout=httpx.Timeout(
                settings.timeout_seconds,
                connect=min(10.0, settings.timeout_seconds),
            ),
            transport=transport,
            follow_redirects=False,
        )

    def close(self):
        self._http.close()

    def complete(self, prompt: str) -> str:
        # 兼容现有 AnswerService。
        return self.generate(prompt).text

    def complete_structured(self, prompt: str) -> str:
        return self.generate(prompt, structured=True).text

    def generate(self, prompt: str, *, structured: bool = False) -> LLMResponse:
        if not prompt.strip():
            raise ValueError("提示词不能为空")

        payload = {
            'model': self.settings.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False,
            'max_tokens': self.settings.max_output_tokens,
        }
        if self.settings.model.lower().startswith('mimo-'):
            payload['max_completion_tokens'] = payload.pop('max_tokens')
            if structured:
                payload['thinking'] = {'type': 'disabled'}
                payload['response_format'] = {'type': 'json_object'}
        try:
            response = self._http.post(
                f"{self.settings.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                },
                json=payload,
            )
        except httpx.TimeoutException:
            raise ModelServiceError(
                "模型请求超时，请稍后重试。",
                status_code=504,
            ) from None
        except httpx.RequestError:
            raise ModelServiceError(
                "无法连接模型服务，请检查网络和接口地址。"
            ) from None

        status = response.status_code

        if status == 401:
            raise ModelServiceError(
                "模型认证失败，请检查密钥与接口地址是否匹配。"
            )

        if status == 403:
            raise ModelServiceError(
                "没有访问该模型的权限，请检查服务开通状态。"
            )

        if status == 429:
            raise ModelServiceError(
                "模型服务触发限流或额度限制。",
                status_code=503,
            )

        if status >= 500:
            raise ModelServiceError("模型服务暂时不可用。")

        if not response.is_success:
            raise ModelServiceError(
                f"模型请求被拒绝（HTTP {status}），"
                "请检查模型名称、地址和参数兼容性。"
            )

        try:
            body = response.json()
            if body['choices'][0].get('finish_reason') == 'length':
                raise ModelServiceError('模型输出达到长度上限并被截断，请检查输出长度或思考模式配置。')
            content = body["choices"][0]["message"]["content"]

            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty answer")

            usage = body.get("usage")
            if not isinstance(usage, dict):
                usage = {}

            return LLMResponse(
                text=content.strip(),
                model=optional_text(body.get("model")),
                request_id=optional_text(body.get("id")),
                prompt_tokens=optional_count(usage.get("prompt_tokens")),
                completion_tokens=optional_count(
                    usage.get("completion_tokens")
                ),
                total_tokens=optional_count(usage.get("total_tokens")),
            )
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelServiceError(
                "模型返回格式异常或回答为空。"
            ) from None
