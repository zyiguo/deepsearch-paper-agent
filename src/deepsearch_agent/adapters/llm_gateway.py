from deepsearch_agent.core.models import ModelServiceError, LLMResponse, optional_count
import asyncio
import base64
from pathlib import Path

import httpx
from deepsearch_agent.core.cancellation import current_token, ResearchCancelled

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.governed_calls import metered


def optional_text(value):
    return value if isinstance(value, str) and value else None


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
        self._transport = transport
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

    def complete_images(self, prompt: str, images: list[Path], *, structured: bool = False) -> str:
        if not images or not prompt.strip():
            raise ValueError('视觉请求需要提示词和图片')
        content = [{'type': 'text', 'text': prompt}]
        for path in images:
            data = Path(path).read_bytes()
            if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 10 * 1024 * 1024:
                raise ValueError('视觉输入必须是小于 10 MB 的 PNG')
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii'),
                'detail': 'high',
            }})
        return self.generate_messages([{'role': 'user', 'content': content}], structured=structured).text

    def generate(self, prompt: str, *, structured: bool = False) -> LLMResponse:
        if not prompt.strip():
            raise ValueError("提示词不能为空")

        return self.generate_messages([{'role': 'user', 'content': prompt}], structured=structured)

    def complete_messages(self, messages: list[dict]) -> str:
        return self.generate_messages(messages).text

    @metered('llm')
    def generate_messages(self, messages: list[dict], *, structured: bool = False) -> LLMResponse:
        def valid_content(content):
            if isinstance(content, str):
                return bool(content.strip())
            if not isinstance(content, list) or not content:
                return False
            for part in content:
                if not isinstance(part, dict):
                    return False
                if part.get('type') == 'text':
                    if not isinstance(part.get('text'), str) or not part['text'].strip():
                        return False
                elif part.get('type') == 'image_url':
                    image = part.get('image_url')
                    if not isinstance(image, dict) or not isinstance(image.get('url'), str) or not image['url'].startswith('data:image/png;base64,'):
                        return False
                else:
                    return False
            return True
        if not messages or any(
            m.get('role') not in {'system', 'user', 'assistant'}
            or not valid_content(m.get('content'))
            for m in messages
        ):
            raise ValueError('消息必须包含有效角色和非空内容')
        payload = {
            'model': self.settings.model,
            'messages': messages,
            'stream': False,
            'max_tokens': self.settings.max_output_tokens,
        }
        if self.settings.model.lower().startswith('mimo-'):
            payload['max_completion_tokens'] = payload.pop('max_tokens')
            if structured:
                payload['thinking'] = {'type': 'disabled'}
                payload['response_format'] = {'type': 'json_object'}
        token = current_token.get()
        if token is not None:
            token.check()
        try:
            if token is None:
                response = self._http.post(
                    f"{self.settings.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}"}, json=payload)
            else:
                async def send():
                    async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.timeout_seconds,
                                              connect=min(10.0, self.settings.timeout_seconds)),
                                              transport=self._transport, follow_redirects=False) as client:
                        task = asyncio.current_task()
                        token.bind_task(asyncio.get_running_loop(), task)
                        try:
                            return await client.post(
                                f"{self.settings.base_url.rstrip('/')}/chat/completions",
                                headers={"Authorization": f"Bearer {self.settings.api_key}"}, json=payload)
                        finally:
                            token.clear_task()
                try:
                    response = asyncio.run(send())
                except asyncio.CancelledError:
                    raise ResearchCancelled('research_cancelled') from None
                token.check()
        except httpx.TimeoutException:
            raise ModelServiceError(
                "模型请求超时，请稍后重试。",
                status_code=504,
                error_code='timeout',
            ) from None
        except httpx.RequestError:
            raise ModelServiceError(
                "无法连接模型服务，请检查网络和接口地址。", error_code='connection_error',
            ) from None

        status = response.status_code
        decoded = True
        try:
            body = response.json()
        except (ValueError, RecursionError):
            decoded = False
            body = None
        usage = body.get('usage') if isinstance(body, dict) else None
        metadata = {'provider_status_code': status, 'usage': usage}

        if status == 401:
            raise ModelServiceError(
                "模型认证失败，请检查密钥与接口地址是否匹配。",
                error_code='authentication_error', **metadata,
            )

        if status == 403:
            raise ModelServiceError(
                "没有访问该模型的权限，请检查服务开通状态。",
                error_code='permission_denied', **metadata,
            )

        if status == 429:
            raise ModelServiceError(
                "模型服务触发限流或额度限制。",
                status_code=503,
                error_code='rate_limit', **metadata,
            )

        if status >= 500:
            raise ModelServiceError("模型服务暂时不可用。", error_code='server_error', **metadata)

        if not response.is_success:
            raise ModelServiceError(
                f"模型请求被拒绝（HTTP {status}），"
                "请检查模型名称、地址和参数兼容性。",
                error_code='request_rejected', **metadata,
            )

        if not decoded:
            raise ModelServiceError('模型返回的内容不是有效 JSON。', error_code='invalid_json', **metadata)
        try:
            choice = body['choices'][0]
            if not isinstance(choice, dict):
                raise ValueError('invalid choice')
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelServiceError(
                "模型返回格式异常或回答为空。", error_code='invalid_response', **metadata,
            ) from None
        reason = choice.get('finish_reason')
        metadata['finish_reason'] = reason if isinstance(reason, str) else None
        if reason == 'length':
            raise ModelServiceError('模型输出达到长度上限并被截断，请检查输出长度或思考模式配置。',
                                    error_code='output_truncated', **metadata)
        if reason == 'content_filter':
            raise ModelServiceError('模型输出被服务端内容过滤。', error_code='content_filtered', **metadata)
        message = choice.get('message')
        if not isinstance(message, dict) or 'content' not in message:
            raise ModelServiceError('模型返回格式异常或回答为空。', error_code='invalid_response', **metadata)
        content = message['content']
        if content is None or isinstance(content, str) and not content.strip():
            raise ModelServiceError('模型返回格式异常或回答为空。', error_code='empty_response', **metadata)
        if not isinstance(content, str):
            raise ModelServiceError('模型返回格式异常或回答为空。', error_code='invalid_response', **metadata)
        usage = usage if isinstance(usage, dict) else {}
        return LLMResponse(
            text=content.strip(), model=optional_text(body.get('model')),
            request_id=optional_text(body.get('id')),
            prompt_tokens=optional_count(usage.get('prompt_tokens')),
            completion_tokens=optional_count(usage.get('completion_tokens')),
            total_tokens=optional_count(usage.get('total_tokens')),
        )
