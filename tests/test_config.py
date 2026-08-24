from deepsearch_agent.config import AppConfig


def test_default_config_uses_local_pdf_only_defaults() -> None:
    config = AppConfig()

    assert config.app_name == "DeepSearch Paper Agent"
    assert config.allow_web_search is False
    assert config.default_language == "zh"
def test_config_can_be_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEARCH_APP_NAME", "My Research Assistant")
    monkeypatch.setenv("DEEPSEARCH_ALLOW_WEB_SEARCH", "true")
    monkeypatch.setenv("DEEPSEARCH_DEFAULT_LANGUAGE", "en")

    config = AppConfig.from_env()

    assert config.app_name == "My Research Assistant"
    assert config.allow_web_search is True
    assert config.default_language == "en"
def test_config_accepts_common_true_values(monkeypatch) -> None:
    for value in ["true", "TRUE", "1", "yes", "on"]:
        monkeypatch.setenv("DEEPSEARCH_ALLOW_WEB_SEARCH", value)

        config = AppConfig.from_env()

        assert config.allow_web_search is True
def test_default_config_has_llm_defaults() -> None:
    config = AppConfig()

    assert config.llm_base_url == "https://api.openai.com/v1"
    assert config.llm_model == "gpt-4o-mini"
    assert config.llm_api_key_env == "OPENAI_API_KEY"
def test_llm_config_can_be_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEARCH_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("DEEPSEARCH_LLM_MODEL", "qwen3:0.6b")
    monkeypatch.setenv("DEEPSEARCH_LLM_API_KEY_ENV", "OLLAMA_API_KEY")

    config = AppConfig.from_env()

    assert config.llm_base_url == "http://localhost:11434/v1"
    assert config.llm_model == "qwen3:0.6b"
    assert config.llm_api_key_env == "OLLAMA_API_KEY"