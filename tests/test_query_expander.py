"""Tests for the LLM-based query expander."""
from deepsearch_agent.knowledge.query_expander import LLMQueryExpander


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.last_prompt = None

    def complete(self, prompt):
        self.last_prompt = prompt
        return self.response


def test_expand_returns_alternative_queries():
    client = FakeClient('{"queries": ["对比学习训练方法", "encoder 训练策略", "self-supervised 训练"]}')
    expander = LLMQueryExpander(client)

    result = expander.expand('模型怎么训练？')

    assert len(result) == 3
    assert '对比学习训练方法' in result
    assert '模型怎么训练？' not in result  # original not included


def test_expand_with_history_includes_history_in_prompt():
    client = FakeClient('{"queries": ["GPT-4 参数量"]}')
    expander = LLMQueryExpander(client)
    history = [{'role': 'user', 'content': 'GPT-4 是什么？'},
               {'role': 'assistant', 'content': 'GPT-4 是一个大语言模型。'}]

    expander.expand('它的参数量是多少？', history=history)

    assert '历史对话' in client.last_prompt
    assert 'GPT-4' in client.last_prompt


def test_expand_without_history_omits_history_part():
    client = FakeClient('{"queries": ["test"]}')
    expander = LLMQueryExpander(client)

    expander.expand('问题')

    assert '历史对话：' not in client.last_prompt


def test_expand_fallback_on_invalid_json():
    client = FakeClient('not json at all')
    expander = LLMQueryExpander(client)

    assert expander.expand('问题') == []


def test_expand_fallback_on_runtime_error():
    class BrokenClient:
        def complete(self, prompt):
            raise RuntimeError('LLM timeout')
    expander = LLMQueryExpander(BrokenClient())

    assert expander.expand('问题') == []


def test_expand_strips_code_fences():
    client = FakeClient('```json\n{"queries": ["改写一", "改写二"]}\n```')
    expander = LLMQueryExpander(client)

    result = expander.expand('问题')

    assert len(result) == 2
    assert result[0] == '改写一'


def test_expand_dedup_against_original():
    # LLM returns the original query verbatim — should be filtered out.
    client = FakeClient('{"queries": ["原问题", "改写一", "改写二"]}')
    expander = LLMQueryExpander(client)

    result = expander.expand('原问题')

    assert '原问题' not in result
    assert len(result) == 2


def test_expand_caps_at_5():
    client = FakeClient('{"queries": ["q1", "q2", "q3", "q4", "q5", "q6"]}')
    expander = LLMQueryExpander(client)

    result = expander.expand('原问题')

    assert len(result) == 5


def test_expand_truncates_long_queries():
    long_q = '这是一个非常非常非常非常非常非常非常非常非常非常非常非常非常长的查询' * 3
    client = FakeClient(f'{{"queries": ["{long_q}", "正常改写"]}}')
    expander = LLMQueryExpander(client)

    result = expander.expand('原问题')

    assert len(result[0]) <= 50
    assert result[1] == '正常改写'
