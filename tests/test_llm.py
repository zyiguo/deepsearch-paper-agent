from deepsearch_agent.llm import FakeLLMClient


def test_fake_llm_client_returns_configured_response() -> None:
    client = FakeLLMClient(response="这是一个测试回答")

    answer = client.complete("请总结这篇论文")

    assert answer == "这是一个测试回答"