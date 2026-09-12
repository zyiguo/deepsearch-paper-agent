import os
from urllib.parse import urlsplit

import httpx


class ModelServiceError(RuntimeError):
    """可安全返回给前端的模型服务错误。"""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class QwenClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        api_key = api_key.strip()
        base_url = base_url.strip().rstrip("/")
        model = model.strip()

        if not api_key:
            raise ValueError("未配置 DASHSCOPE_API_KEY")

        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("QWEN_BASE_URL 必须是合法的 HTTPS 接口基础地址")

        if not model:
            raise ValueError("未配置 QWEN_MODEL")

        self._api_key = api_key
        self._base_url = base_url
        self.model = model
        self._transport = transport

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=os.getenv("QWEN_BASE_URL", ""),
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
        )

    def complete(self, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("提示词不能为空")

        try:
            with httpx.Client(
                timeout=httpx.Timeout(60.0, connect=10.0),
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "stream": False,
                        "max_tokens": 1024,
                    },
                )
        except httpx.TimeoutException:
            raise ModelServiceError(
                "Qwen 请求超时，请稍后重试。",
                status_code=504,
            ) from None
        except httpx.RequestError:
            raise ModelServiceError(
                "无法连接 Qwen，请检查网络和接口地址。"
            ) from None

        if response.status_code == 401:
            raise ModelServiceError(
                "Qwen 认证失败，请检查 API Key 与接口地域。"
            )

        if response.status_code == 403:
            raise ModelServiceError(
                "Qwen 无权访问，请检查模型权限和服务开通状态。"
            )

        if response.status_code == 429:
            raise ModelServiceError(
                "Qwen 触发限流或额度限制，请检查控制台后重试。",
                status_code=503,
            )

        if response.status_code >= 500:
            raise ModelServiceError("Qwen 服务暂时不可用。")

        if not response.is_success:
            raise ModelServiceError(
                f"Qwen 请求被拒绝（HTTP {response.status_code}），"
                "请检查模型名称、接口地址和请求配置。"
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]

            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")

            return content.strip()
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelServiceError(
                "Qwen 返回格式异常或回答为空。"
            ) from None