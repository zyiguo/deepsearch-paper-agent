import json
import logging

from deepsearch_agent.qa import AnswerService
from test_supervisor import Script, Library, decision


def diagnostic(caplog):
    records = [r for r in caplog.records if r.name == 'deepsearch_agent.research_graph']
    assert len(records) == 1
    return json.loads(records[0].getMessage().split(' ', 1)[1])


def test_early_finish_logs_counts_without_private_text(caplog):
    with caplog.at_level(logging.WARNING):
        steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['signal']}),
                 ('Supervisor', decision('finish'))]
        AnswerService(Script(steps), Library(), use_graph=True).answer('PRIVATE_QUESTION')
    data = diagnostic(caplog)
    assert data['reason'] == 'finish_before_verification'
    assert data['evidence_count'] == 1
    assert data['claim_count'] == 0
    assert data['reviews'] == 0
    assert 'PRIVATE_QUESTION' not in caplog.text


def test_rejected_draft_reports_zero_supported(caplog):
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['PRIVATE_QUERY']}),
             ('Supervisor', decision('draft')),
             ('Answerer', {'claims': [{'text': 'PRIVATE_CLAIM', 'citations': [1]}]}),
             ('Verifier', {'supported': [], 'queries': [], 'issues': ['PRIVATE_ISSUE']}),
             ('Supervisor', decision('finish'))]
    with caplog.at_level(logging.WARNING):
        AnswerService(Script(steps), Library(), use_graph=True).answer('PRIVATE_QUESTION')
    data = diagnostic(caplog)
    assert data['reason'] == 'no_supported_claims'
    assert data['claim_count'] == 1 and data['supported_count'] == 0
    assert data['evidence_count'] == 1 and data['reviews'] == 1
    assert 'PRIVATE_' not in caplog.text


def test_success_does_not_emit_failure_record(caplog):
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['signal']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims': [{'text': '白光', 'citations': [1]}]}),
             ('Verifier', {'supported': [0], 'queries': []}), ('Supervisor', decision('finish'))]
    with caplog.at_level(logging.WARNING):
        result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号？')
    assert result.text == '白光 [1]'
    assert not [r for r in caplog.records if r.name == 'deepsearch_agent.research_graph']
