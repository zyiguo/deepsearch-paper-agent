import json
from pathlib import Path

import pytest

from deepsearch_agent.workspace.skills import SkillRegistry, SkillError


def write_skill(root, name='compare', body='Compare the reported methods.', description='Compare papers.'):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\n{body}', encoding='utf-8')
    return directory / 'SKILL.md'


def test_catalog_is_metadata_only_and_body_is_loaded_on_demand(tmp_path):
    path = write_skill(tmp_path, body='PRIVATE BODY MARKER')
    session = SkillRegistry(tmp_path).session()
    assert session.catalog == [{'name': 'compare', 'description': 'Compare papers.'}]
    assert 'PRIVATE BODY' not in session.prompt()
    session.load(['compare', 'compare', '../outside'])
    assert len(session.used) == 1
    assert 'PRIVATE BODY MARKER' in session.prompt()
    version = session.used[0]['sha256']
    path.write_text(path.read_text().replace('PRIVATE BODY MARKER', 'NEW BODY'), encoding='utf-8')
    assert 'PRIVATE BODY MARKER' in session.prompt()
    fresh = SkillRegistry(tmp_path).session(['compare'])
    assert 'NEW BODY' in fresh.prompt() and fresh.used[0]['sha256'] != version
    path.unlink()
    assert SkillRegistry(tmp_path).session().catalog == []


def test_explicit_disable_unknown_and_invalid_metadata(tmp_path):
    write_skill(tmp_path)
    assert SkillRegistry(tmp_path).session([]).catalog == []
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path).session(['../compare'])
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path).session(['missing'])
    write_skill(tmp_path, 'invalid', description='[not, text]')
    snapshot = SkillRegistry(tmp_path).session()
    assert [x['name'] for x in snapshot.catalog] == ['compare']
    assert snapshot.warnings


def test_budget_and_changed_file_fail_closed(tmp_path):
    path = write_skill(tmp_path, body='word ' * 4000)
    session = SkillRegistry(tmp_path).session(max_tokens=100)
    session.load(['compare'])
    assert not session.used and session.warnings
    session = SkillRegistry(tmp_path).session()
    path.write_text('---\nname: compare\ndescription: Changed\n---\nchanged', encoding='utf-8')
    session.load(['compare'])
    assert not session.used


def test_symlink_escape_is_not_discovered(tmp_path):
    outside = tmp_path / 'outside'
    write_skill(outside)
    root = tmp_path / 'skills'
    root.mkdir()
    try:
        (root / 'compare').symlink_to(outside / 'compare', target_is_directory=True)
    except OSError:
        pytest.skip('Symlink permission unavailable')
    assert SkillRegistry(root).session().catalog == []


def test_supervisor_loads_skill_without_making_it_evidence(tmp_path):
    from deepsearch_experiments.legacy.qa import AnswerService
    from test_research_graph import Client, Library
    write_skill(tmp_path, body='SKILL METHOD MARKER. Never invent evidence.')
    prompts = []
    class SelectingClient(Client):
        def complete(self, prompt):
            prompts.append(prompt)
            raw = super().complete(prompt)
            if prompt.startswith('你是 Supervisor。'):
                data = json.loads(raw)
                data['skill_ids'] = ['compare', '../outside']
                return json.dumps(data)
            return raw
    client = SelectingClient([{'queries': ['灯塔']},
                              {'claims': [{'text': '白光', 'citations': [1]}]},
                              {'supported': [0], 'queries': []}])
    service = AnswerService(client, Library(), use_graph=True, skill_registry=SkillRegistry(tmp_path))
    result = service.answer('比较灯塔信号')
    assert 'SKILL METHOD MARKER' not in prompts[0]
    assert 'Compare papers.' in prompts[0]
    assert 'SKILL METHOD MARKER' in next(p for p in prompts if p.startswith('你是 Researcher。'))
    assert 'SKILL METHOD MARKER' not in next(p for p in prompts if p.startswith('你是 Verifier。'))
    assert result.text == '白光 [1]'
    assert result.skills_used[0]['name'] == 'compare'
    assert result.sources[0].source_path == 'a.pdf'


def test_explicit_skill_basic_qa_and_request_isolation(tmp_path):
    from deepsearch_experiments.legacy.qa import AnswerService
    from test_research_graph import Library
    write_skill(tmp_path, body='SKILL METHOD MARKER')
    class Client:
        def complete(self, prompt):
            return 'skill' if 'SKILL METHOD MARKER' in prompt else 'plain'
    service = AnswerService(Client(), Library(), skill_registry=SkillRegistry(tmp_path))
    assert service.answer('q', skill_ids=['compare']).text == 'skill'
    assert service.answer('q', skill_ids=[]).text == 'plain'
    assert service.answer('q').text == 'plain'


def test_skill_api_validation_and_hot_reload(tmp_path):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.demo import DEMO_QUESTIONS
    root = tmp_path / 'skills'
    app = create_app(index_path=tmp_path / 'index.json', llm_settings=LLMSettings(), skills_path=root)
    with TestClient(app) as client:
        assert client.get('/api/skills').json()['skills'] == []
        write_skill(root)
        assert client.get('/api/skills').json()['skills'][0]['name'] == 'compare'
        session = client.post('/api/sessions', json={'title': 'Skills'}).json()['id']
        body = {'question': DEMO_QUESTIONS[0], 'skill_ids': ['../outside']}
        assert client.post('/api/ask', json=body).status_code == 422
        assert client.post(f'/api/sessions/{session}/turns/stream', json=body).status_code == 422
        client.post('/api/demo/ingest')
        body['skill_ids'] = ['compare']
        response = client.post(f'/api/sessions/{session}/turns', json=body)
        assert response.status_code == 201, response.text
        assert response.json()['answer']['skills_used'][0]['name'] == 'compare'
        assert client.get(f'/api/sessions/{session}').json()['turns'][0]['answer']['skills_used']
