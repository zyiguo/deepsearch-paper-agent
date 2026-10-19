import importlib.util
import json
from pathlib import Path
import sys
import pytest
from deepsearch_agent.core.models import LLMResponse
from test_evidence_paths import candidate, EVIDENCE, review


def module():
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    sys.path.insert(0,str(scripts))
    try:
        spec=importlib.util.spec_from_file_location('gap_audit_test',scripts/'audit_deferred_gap_paths.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
    finally:sys.path.remove(str(scripts))


def test_counterfactual_uses_independent_graph_verifier_and_accounts_cost(monkeypatch):
    m=module()
    class Client:
        def __init__(self,settings):pass
        def generate(self,prompt):return LLMResponse(text=json.dumps(review()),total_tokens=123)
        def close(self):pass
    monkeypatch.setattr(m,'OpenAICompatibleClient',Client)
    row={'question':'Where was Alice born?','requirements':['birthplace'], 'evidence':EVIDENCE,
         'all_candidates':[candidate()],'decision':{'candidate_index':0,'path_index':0}}
    result=m.counterfactual_review(row,None)
    assert result['status']=='ok' and result['accepts_deferred_candidate']
    assert result['deferred_path_status']=='supported' and result['reported_total_tokens']==123
    assert len(result['calls'])==1 and result['gap_state_removed']


def test_blinded_judge_cannot_claim_support_without_valid_quote():
    m=module()
    value={'candidate_supported':'supported','path_sufficient':True,'missing_requirements':[],
           'supporting_quotes':[{'citation':99,'quote':'Alice was born in Paris.'}], 'explanation':'Supported'}
    with pytest.raises(ValueError):
        m.validate_judgment(m.Audit.model_validate(value),{'evidence':EVIDENCE,'requirements':['birthplace']})
    value['supporting_quotes']=[{'citation':1,'quote':'Alice was born in Paris.'}]
    m.validate_judgment(m.Audit.model_validate(value),{'evidence':EVIDENCE,'requirements':['birthplace']})
