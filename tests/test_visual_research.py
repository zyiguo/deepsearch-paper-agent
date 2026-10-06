import json

import pytest

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.llm_gateway import ModelServiceError
from deepsearch_agent.qa import AnswerService


class TextClient:
    def complete(self, prompt):
        if prompt.startswith('你是 Supervisor。'):
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            action = 'research' if data['research_rounds'] == 0 else ('finish' if data['verified'] else 'draft')
            return json.dumps({'action': action, 'tasks': [], 'instruction': ''})
        if prompt.startswith('你是 Researcher。'):
            return json.dumps({'queries': ['Voltage']})
        pytest.fail('Visual claims must not be drafted or verified by the text client')


class Library:
    def search(self, query, top_k=5):
        return [TextChunk('Table 1: Voltage 42 V', 'paper.pdf', 3, 0, evidence_kind='table', visual_pages=[3])]


def test_both_answerer_and_verifier_receive_original_page_references():
    calls = []
    class Provider:
        def complete(self, prompt, evidence, *, structured=True):
            calls.append(prompt)
            assert evidence[0]['visual_pages'] == [3] and evidence[0]['evidence_kind'] == 'table'
            if prompt.startswith('你是 Answerer。'):
                return json.dumps({'claims': [{'text': 'Voltage is 42 V', 'citations': [1]}]})
            assert prompt.startswith('你是 Verifier。')
            return json.dumps({'supported': [0], 'queries': []})
    result = AnswerService(TextClient(), Library(), use_graph=True, visual_evidence=Provider()).answer('Voltage?')
    assert len(calls) == 2
    assert result.text == 'Voltage is 42 V [1]'
    assert result.sources[0].evidence_kind == 'table' and result.sources[0].visual_pages == [3]


def test_graph_refuses_to_verify_visual_descriptions_without_visual_provider():
    with pytest.raises(ModelServiceError, match='视觉模型'):
        AnswerService(TextClient(), Library(), use_graph=True).answer('Voltage?')
