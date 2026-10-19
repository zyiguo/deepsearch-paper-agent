import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class LLMSettings:
    backend: str = "demo"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1024
    context_window: int = 8192

    def __post_init__(self):
        if self.backend not in {"demo", "openai_compatible"}:
            raise ValueError(
                "LLM_BACKEND 只支持 demo 或 openai_compatible"
            )

        if (
            not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("LLM_TIMEOUT_SECONDS 必须在 0 到 300 秒之间")

        if not 1 <= self.max_output_tokens <= 131072:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS 超出允许范围")

        if not 1024 <= self.context_window <= 1_048_576:
            raise ValueError("LLM_CONTEXT_WINDOW 必须在 1024 到 1048576 之间")

        if self.backend == "demo":
            return

        if not self.api_key.strip():
            raise ValueError("未配置 LLM_API_KEY")

        if not self.model.strip():
            raise ValueError("未配置 LLM_MODEL")

        url = urlsplit(self.base_url)

        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/").endswith("/chat/completions")
        ):
            raise ValueError(
                "LLM_BASE_URL 必须是 HTTPS 基础地址，"
                "不能包含 /chat/completions、账号密码或查询参数"
            )

    @classmethod
    def load(
        cls,
        env_file: Path,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        # 本地文件优先，但不向 os.environ 写入任何内容。
        values = dict(os.environ if environ is None else environ)

        if env_file.exists():
            local_values = dotenv_values(
                env_file,
                encoding="utf-8-sig",
                interpolate=False,
            )
            values.update({
                key: value
                for key, value in local_values.items()
                if value is not None
            })

        def text(name, default=""):
            return values.get(name, default).strip()

        try:
            timeout = float(text("LLM_TIMEOUT_SECONDS", "60"))
            max_tokens = int(text("LLM_MAX_OUTPUT_TOKENS", "1024"))
            context_window = int(text("LLM_CONTEXT_WINDOW", "8192"))
        except ValueError:
            raise ValueError("模型超时、输出长度和上下文窗口配置必须是有效数字") from None

        return cls(
            backend=text("LLM_BACKEND", "demo").lower(),
            api_key=text("LLM_API_KEY"),
            base_url=text("LLM_BASE_URL").rstrip("/"),
            model=text("LLM_MODEL"),
            timeout_seconds=timeout,
            max_output_tokens=max_tokens,
            context_window=context_window,
        )
