from pathlib import Path
import json
import pytest
from deepsearch_experiments.infrastructure_e2e import Acceptance


def test_acceptance_never_overwrites_existing_output(tmp_path):
    target = tmp_path / 'review'
    target.mkdir()
    review = target / 'report.json'
    review.write_text('human review', encoding='utf-8')
    with pytest.raises(FileExistsError):
        Acceptance(tmp_path, target)
    assert review.read_text(encoding='utf-8') == 'human review'


def test_cleanup_refuses_a_volume_owned_by_another_project(tmp_path, monkeypatch):
    run = Acceptance(tmp_path, tmp_path / 'new-output')
    run.created = True
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ['volume', 'ls']:
            return 'foreign-volume\n'
        if args[:2] == ['volume', 'inspect']:
            return json.dumps([{'Labels': {'com.docker.compose.project': 'another-project'}}])
        raise AssertionError('Unexpected mutation')
    monkeypatch.setattr(run, 'run_command', command)
    monkeypatch.setattr(run, 'compose_command', lambda args, **kw: calls.append(args))
    with pytest.raises(AssertionError):
        run.cleanup()
    assert not any(args[0] == 'down' or args[:2] == ['volume','rm'] for args in calls)
