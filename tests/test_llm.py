from deepsearch_agent.adapters.demo_llm import FakeLLMClient
from deepsearch_agent.core.ports import LLMClient


def test_fake_llm_client_returns_configured_response() -> None:
    client = FakeLLMClient(response="这是一个测试回答")

    answer = client.complete("请总结这篇论文")

    assert answer == "这是一个测试回答"
def test_fake_llm_client_satisfies_llm_client_protocol() -> None:
    client: LLMClient = FakeLLMClient(response="ok")

    assert isinstance(client, LLMClient)
    assert client.complete("ping") == "ok"