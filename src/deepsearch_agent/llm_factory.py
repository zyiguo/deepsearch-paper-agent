from deepsearch_agent.demo import build_demo_llm
from deepsearch_agent.llm import FakeLLMClient
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_gateway import OpenAICompatibleClient


def build_llm(
    settings: LLMSettings,
) -> FakeLLMClient | OpenAICompatibleClient:
    if settings.backend == "demo":
        return build_demo_llm()

    return OpenAICompatibleClient(settings)