"""Configuration for DeepSearch Paper Agent."""

import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    app_name: str = "DeepSearch Paper Agent"
    allow_web_search: bool = False
    default_language: str = "zh"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_key_env: str = "OPENAI_API_KEY"

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            app_name=os.environ.get("DEEPSEARCH_APP_NAME", "DeepSearch Paper Agent"),
            allow_web_search=_parse_bool(
                os.environ.get("DEEPSEARCH_ALLOW_WEB_SEARCH", "false")
            ),
            default_language=os.environ.get("DEEPSEARCH_DEFAULT_LANGUAGE", "zh"),
            llm_base_url=os.environ.get(
                "DEEPSEARCH_LLM_BASE_URL", "https://api.openai.com/v1"
            ),
            llm_model=os.environ.get("DEEPSEARCH_LLM_MODEL", "gpt-4o-mini"),
            llm_api_key_env=os.environ.get(
                "DEEPSEARCH_LLM_API_KEY_ENV", "OPENAI_API_KEY"
            ),
        )


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "on"}