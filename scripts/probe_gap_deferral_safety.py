"""Explicit synthetic counterexample: changed claims can correct a path without new quotes."""
from pathlib import Path
from copy import deepcopy
from evaluate_multicandidate_paths import write
from deepsearch_agent.core.evidence_paths import inspect_candidates, resolve_candidates
from deepsearch_agent.core.verification_gaps import record_gaps, filter_review_paths


def probe():
    evidence=[{'id':1,'source':'synthetic-fixture','text':'Alice was born in Paris.'}]
    candidate={'answer':'Paris','interpretation':'The birthplace of Alice', 'paths':[{'steps':[{
        'requirement_index':0,'claim':'Alice traveled to Paris.', 'citation':1,'quote':'Alice was born in Paris.'}]}]}
    # Birthplace does not establish a travel event. This old submitted claim is
    # unsupported; no external knowledge or semantic gold inference is needed.
    old_review={'paths':[{'candidate_index':0,'path_index':0,'verdict':'unknown',
        'supported_requirements':[],'reason':'The quote establishes birth, not the claimed travel event.'}],
        'selected_candidate':None,'answer_resolved':False,'issues':['Path does not state the required birth relation.'],'queries':[]}
    records=inspect_candidates([candidate],evidence,hard_pruning=True)
    records,_=resolve_candidates(records,old_review,1)
    gaps=record_gaps(records,old_review,evidence,1)
    corrected=deepcopy(candidate)
    corrected['paths'][0]['steps'][0]['claim']='Alice was born in Paris.'
    refreshed=inspect_candidates([corrected],evidence,hard_pruning=True)
    payload=[{'candidate_index':0,'answer':'Paris','interpretation':candidate['interpretation'],
              'paths':[{'path_index':0,'steps':corrected['paths'][0]['steps']}]}]
    filtered,deferred=filter_review_paths(payload,refreshed,evidence,gaps)
    assert deferred and not filtered[0]['paths']
    # Constructive witness: the corrected claim equals the evidence verbatim,
    # explicitly states the required relation, and yields the candidate answer.
    assert corrected['paths'][0]['steps'][0]['claim']==evidence[0]['text']
    return {'kind':'synthetic-constructive-counterexample-not-natural-evaluation',
            'question':'Where was Alice born?', 'requirement':'Establish Alice birthplace',
            'evidence':evidence,'previous_candidate':candidate,'previous_review':old_review,
            'corrected_candidate':corrected,'deferred':deferred,
            'false_defer_demonstrated':True,'candidate_deleted':False,
            'cause':'Witness identity excludes claim text; corrected reasoning with identical quote is deferred.',
            'scope':'Demonstrates a possible false deferral, not its prevalence in real benchmarks. No model calls or human labels assigned.'}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();write(args.output,probe());print('Synthetic corrected-claim counterexample: false deferral reproduced.')
