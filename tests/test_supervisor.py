import json
import pytest
from deepsearch_agent.qa import AnswerService
from deepsearch_agent.chunking import TextChunk


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
    def complete(self, prompt):
        role, output = next(self.steps)
        assert prompt.startswith(f'你是 {role}。')
        return json.dumps(output, ensure_ascii=False)


class Library:
    def search(self, query, top_k=5):
        return [TextChunk(text='十二秒闪白光', source_path='a.pdf', page_number=1, chunk_index=0)]


def decision(action):
    return {'action':action, 'tasks':[{'question':'灯塔信号', 'status':'pending'}], 'instruction':'核查灯塔信号'}


def test_supervisor_revises_without_research_and_requires_reverification():
    steps = [
        ('Supervisor', decision('research')),
        ('Researcher', {'queries':['信号']}),
        ('Supervisor', decision('draft')),
        ('Answerer', {'claims':[{'text':'红光','citations':[1]}]}),
        ('Verifier', {'supported':[], 'queries':[], 'issues':['应为白光']}),
        ('Supervisor', decision('revise')),
        ('Answerer', {'claims':[{'text':'白光','citations':[1]}]}),
        ('Verifier', {'supported':[0], 'queries':[], 'issues':[]}),
        ('Supervisor', decision('finish')),
    ]
    result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号？')
    assert result.text == '白光 [1]'


def test_supervisor_cannot_finish_without_verified_draft():
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['信号']}),
             ('Supervisor', decision('finish'))]
    result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号？')
    assert '无法' in result.text and result.sources == []


@pytest.mark.parametrize('early_action', ['stop', 'finish'])
def test_first_search_cannot_be_skipped_by_supervisor(early_action):
    class CheckedLibrary(Library):
        calls = 0
        def search(self, query, top_k=5):
            self.calls += 1
            return super().search(query, top_k)
    kb = CheckedLibrary()
    steps = [('Supervisor', decision(early_action)), ('Researcher', {'queries': ['信号']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims': [{'text': '白光', 'citations': [1]}]}),
             ('Verifier', {'supported': [0], 'queries': []}), ('Supervisor', decision('finish'))]
    result = AnswerService(Script(steps), kb, use_graph=True).answer('信号？')
    assert kb.calls == 1
    assert result.text == '白光 [1]'


def test_stop_is_still_allowed_after_empty_first_search():
    class EmptyLibrary:
        def search(self, query, top_k=5):
            return []
    steps = [('Supervisor', decision('stop')), ('Researcher', {'queries': ['信号']}),
             ('Supervisor', decision('stop'))]
    result = AnswerService(Script(steps), EmptyLibrary(), use_graph=True).answer('信号？')
    assert '无法' in result.text and result.sources == []


def test_supervisor_cannot_publish_unsupported_claims():
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries':['信号']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims':[{'text':'红光','citations':[1]}]}),
             ('Verifier', {'supported':[], 'queries':[], 'issues':['无依据']}), ('Supervisor', decision('finish'))]
    result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号？')
    assert '红光' not in result.text and not result.sources


def test_supervisor_dynamic_research_receives_task_and_review_feedback():
    class CheckedScript(Script):
        def complete(self, prompt):
            if prompt.startswith('你是 Researcher。'):
                data = json.loads(prompt.split('\ndata: ', 1)[1])
                assert data['tasks'][0]['question'] == '灯塔信号'
                assert data['instruction'] == '核查灯塔信号'
            if prompt.startswith('你是 Answerer。'):
                data = json.loads(prompt.split('\ndata: ', 1)[1])
                if data['previous_draft']:
                    assert '查间隔' in data['review_issues']
            return super().complete(prompt)
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries':['信号']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims':[{'text':'白光','citations':[1]}]}),
             ('Verifier', {'supported':[0], 'queries':['间隔'], 'issues':['查间隔']}),
             ('Supervisor', decision('research')), ('Researcher', {'queries':['间隔']}),
             ('Supervisor', decision('revise')), ('Answerer', {'claims':[{'text':'十二秒白光','citations':[1]}]}),
             ('Verifier', {'supported':[0], 'queries':[], 'issues':[]}), ('Supervisor', decision('finish'))]
    result = AnswerService(CheckedScript(steps), Library(), use_graph=True).answer('信号？')
    assert result.text == '十二秒白光 [1]'


def test_supervisor_repeated_research_stops_without_infinite_loop():
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries':['信号']})]*3
    steps += [('Supervisor', decision('research'))]
    result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号？')
    assert '无法' in result.text and not result.sources


def test_supervisor_unknown_action_fails_closed():
    bad = decision('execute_shell')
    result = AnswerService(Script([('Supervisor', bad), ('Supervisor', bad)]), Library(), use_graph=True).answer('信号？')
    assert '格式校验' in result.text and not result.sources
