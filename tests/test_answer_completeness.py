"""Offline regressions: an answerable omission must cause a verified repair."""
import json

import pytest

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_experiments.legacy.qa import AnswerService


class Library:
    def search(self, query, top_k=5):
        return [TextChunk(text='AX-247 samples at 100 Hz. The experiment uses two GPUs.',
                          source_path='fixture.pdf', page_number=1, chunk_index=0)]


class Script:
    def __init__(self, *, terminal='finish', persistent=False, citation=1):
        self.terminal, self.persistent, self.citation = terminal, persistent, citation
        self.drafts = 0
        self.searches = 0

    def complete(self, prompt):
        data = json.loads(prompt.split('\ndata: ', 1)[1])
        if prompt.startswith('你是 Supervisor。'):
            action = 'research' if not data['research_rounds'] else 'draft' if not self.drafts else self.terminal
            output = {'action': action, 'tasks': [], 'instruction': '回答问题'}
        elif prompt.startswith('你是 Researcher。'):
            self.searches += 1
            output = {'queries': ['AX-247']}
        elif prompt.startswith('你是 Answerer。'):
            self.drafts += 1
            claims = [{'text': '采样频率为 100 Hz。', 'citations': [1]}]
            if self.drafts > 1 and not self.persistent:
                assert data['missing_answerable'][0]['question'] == 'GPU 数量'
                claims.append({'text': '实验使用两块 GPU。', 'citations': [1]})
            output = {'claims': claims}
        else:
            assert prompt.startswith('你是 Verifier。')
            output = {'supported': list(range(len(data['claims']))), 'queries': [], 'issues': [],
                      'missing_answerable': ([{'question': 'GPU 数量', 'citations': [self.citation]}]
                                             if len(data['claims']) == 1 else [])}
        return json.dumps(output, ensure_ascii=False)


@pytest.mark.parametrize('terminal', ['finish', 'stop'])
def test_answerable_omission_overrides_early_exit_and_is_reverified(terminal):
    client = Script(terminal=terminal)
    result = AnswerService(client, Library(), use_graph=True).answer('采样频率和 GPU 数量？')
    assert '两块 GPU。 [1]' in result.text
    assert len(result.sources) == 1
    assert client.drafts == 2
    assert client.searches == 1  # Existing evidence: no redundant retrieval.


def test_persistent_omission_stops_at_review_limit_without_inventing_answer():
    client = Script(persistent=True)
    result = AnswerService(client, Library(), use_graph=True).answer('采样频率和 GPU 数量？')
    assert client.drafts == 3
    assert '两块' not in result.text
    assert 'GPU 数量' in result.text
    assert '未完成' in result.text


def test_invalid_omission_evidence_cannot_trigger_repair_as_if_supported():
    client = Script(citation=99)
    result = AnswerService(client, Library(), use_graph=True).answer('采样频率和 GPU 数量？')
    assert client.drafts == 1
    assert '两块' not in result.text
    assert '无法' in result.text
