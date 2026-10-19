"""Audit observations must not mutate evidence or invent human correctness labels."""
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('badcase_audit', Path(__file__).parents[1] / 'scripts/audit_expanded_badcases.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture_root(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for dataset in ('musique', '2wikimultihopqa'):
        folder = root / 'manifests' / dataset
        folder.mkdir(parents=True)
        (folder / 'all.json').write_text(json.dumps({
            'cases': [{'id': 'case', 'question': 'Which city?'}],
            'gold': [{'id': 'case', 'answers': ['Rome'], 'support_document_ids': ['doc']}]
        }), encoding='utf-8')
    trace = {'dataset': 'musique', 'case_id': 'case', 'benchmark_outcome': 'failure',
             'attempts': [{'attempt': 1, 'prediction': {'status': 'invalid_output', 'answer': '',
                 'retrieved_document_ids': ['doc']}, 'usage': {}, 'details': {},
                 'events': [{'kind': 'mainline.rag.response', 'call_id': 'call', 'role': 'Supervisor',
                     'raw': json.dumps({'action': 'stop', 'tasks': [], 'instruction': '', 'research_rounds': 1})}]}]}
    path = root / 'trace.json'
    path.write_text(json.dumps(trace), encoding='utf-8')
    (root / 'trace-manifest.json').write_text(json.dumps([
        {'path': 'trace.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]), encoding='utf-8')
    return root


def test_schema_replay_is_observation_not_human_label(tmp_path):
    root = fixture_root(tmp_path)
    original = (root / 'trace.json').read_bytes()
    destination = tmp_path / 'audit'
    summary = module.audit(root, destination)
    row = json.loads((destination / 'failure.jsonl').read_text(encoding='utf-8'))
    assert summary['new_model_calls'] == 0
    assert row['human_verdict'] is None and row['human_root_causes'] is None
    labels = {x['label'] for x in row['observations']}
    assert 'terminal_schema_rejection_replayed' in labels
    assert 'offline_remove_extra_research_rounds_validates' in labels
    assert (root / 'trace.json').read_bytes() == original


def test_modified_trace_rejected_before_output(tmp_path):
    root = fixture_root(tmp_path)
    with (root / 'trace.json').open('a', encoding='utf-8') as handle:
        handle.write(' ')
    with pytest.raises(ValueError, match='integrity'):
        module.audit(root, tmp_path / 'audit')
    assert not (tmp_path / 'audit').exists()


def test_existing_audit_never_overwritten(tmp_path):
    root = fixture_root(tmp_path)
    destination = tmp_path / 'audit'
    destination.mkdir()
    marker = destination / 'review.json'
    marker.write_text('preserve', encoding='utf-8')
    with pytest.raises(FileExistsError):
        module.audit(root, destination)
    assert marker.read_text(encoding='utf-8') == 'preserve'
