import os

import pytest

from deepsearch_agent.agent_config import AgentSettings


def test_load_defaults_and_disable_without_environment_mutation(tmp_path):
    before = dict(os.environ)
    path = tmp_path / '.env.agent'
    assert AgentSettings.load(path, environ={}).enabled
    path.write_text('AGENTIC_RAG_ENABLED=false\nAGENT_MAX_TOOL_CALLS=5\n', encoding='utf-8')
    settings = AgentSettings.load(path, environ={'AGENTIC_RAG_ENABLED': 'true'})
    assert not settings.enabled
    assert settings.max_tool_calls == 5
    assert dict(os.environ) == before


@pytest.mark.parametrize('kwargs', [dict(enabled='false'), dict(max_tool_calls=True),
    dict(max_ocr_calls=-1), dict(timeout_seconds=float('nan')), dict(max_images=17)])
def test_invalid_agent_limits(kwargs):
    with pytest.raises(ValueError):
        AgentSettings(**kwargs)
