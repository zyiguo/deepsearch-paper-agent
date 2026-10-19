"""Independent credentials and limits for the page-reading model."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

from deepsearch_agent.adapters.llm_config import LLMSettings


@dataclass(frozen=True)
class VisionSettings:
    backend: str = 'disabled'
    llm: LLMSettings | None = None
    max_images: int = 8
    image_token_budget: int = 2500

    def __post_init__(self):
        if self.backend not in {'disabled', 'openai_compatible'}:
            raise ValueError('VISION_BACKEND 必须是 disabled 或 openai_compatible')
        if self.backend == 'openai_compatible' and (self.llm is None or self.llm.backend != 'openai_compatible'):
            raise ValueError('视觉模型需要有效的接口配置')
        if type(self.max_images) is not int or not 1 <= self.max_images <= 16:
            raise ValueError('VISION_MAX_IMAGES 必须在 1 到 16 之间')
        if type(self.image_token_budget) is not int or not 1000 <= self.image_token_budget <= 16000:
            raise ValueError('VISION_IMAGE_TOKEN_BUDGET 必须在 1000 到 16000 之间')

    @classmethod
    def load(cls, path: Path, *, environ: Mapping[str, str] | None = None,
             shared_llm: LLMSettings | None = None):
        values = dict(os.environ if environ is None else environ)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path, encoding='utf-8-sig', interpolate=False).items() if v is not None})
        try:
            backend = values.get('VISION_BACKEND', 'disabled').strip().lower()
            reuse = values.get('VISION_USE_LLM_CREDENTIALS', 'false').strip().lower()
            if reuse not in {'true', 'false'}:
                raise ValueError('VISION_USE_LLM_CREDENTIALS 必须为 true 或 false')
            llm = None
            if backend == 'openai_compatible':
                def value(name, default=''):
                    return values.get(f'VISION_{name}', default).strip()
                shared = shared_llm if reuse == 'true' else None
                if reuse == 'true' and (shared is None or shared.backend != 'openai_compatible'):
                    raise ValueError('共享配置需要真实对话模型')
                api_key = value('API_KEY') or (shared.api_key if shared else '')
                base_url = value('BASE_URL') or (shared.base_url if shared else '')
                llm = LLMSettings(backend=backend, api_key=api_key, base_url=base_url.rstrip('/'),
                                  model=value('MODEL'), timeout_seconds=float(value('TIMEOUT_SECONDS', '120')),
                                  max_output_tokens=int(value('MAX_OUTPUT_TOKENS', '8192')),
                                  context_window=int(value('CONTEXT_WINDOW', '32768')))
            return cls(backend, llm, int(values.get('VISION_MAX_IMAGES', '8')),
                       int(values.get('VISION_IMAGE_TOKEN_BUDGET', '2500')))
        except ValueError:
            raise ValueError('视觉配置无效，请检查 .env.vision 的模型、密钥、地址和数字参数') from None

    @property
    def identity(self):
        llm = self.llm
        value = [llm.base_url, llm.model, 'page-analysis-v2'] if llm else ['disabled']
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:24]
