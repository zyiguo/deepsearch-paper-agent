#context后续即为RAG内容
from deepsearch_agent.core.prompts import SYSTEM_PROMPT, build_qa_prompt


def test_build_qa_prompt_includes_system_prefix_and_question() -> None:
    prompt = build_qa_prompt(question="论文讲了什么？")

    assert SYSTEM_PROMPT in prompt
    assert "论文讲了什么？" in prompt


def test_build_qa_prompt_includes_context_when_provided() -> None:
    prompt = build_qa_prompt(question="方法是什么？", context="论文使用 RAG")

    assert SYSTEM_PROMPT in prompt
    assert "论文使用 RAG" in prompt
    assert "方法是什么？" in prompt