"""Observable execution groups, deliberately separate from correctness/root-cause labels."""


def classify(status, *, pipeline_outcome=None, answer_observed=False, error_type=None,
             error_diagnostics=None, last_role=None):
    stage = {'Supervisor': 'planning', 'Researcher': 'child_research',
             'Answerer': 'generation', 'Verifier': 'verification'}.get(last_role, 'unknown')
    group, outcome, reason = 'unknown', 'unclassified', 'no_internal_verdict'
    if status == 'recording_error':
        outcome, stage, reason = 'recording_error', 'recording', 'trace_write_failed'
    elif status == 'running':
        outcome, reason = 'unfinished', 'run_not_finalized'
    elif status == 'error':
        group = 'failure'
        cancelled = error_type in {'ResearchCancelled', 'CancelledError', 'KeyboardInterrupt'}
        outcome = 'cancelled' if cancelled else ('model_service_error' if error_diagnostics else 'execution_error')
        reason = 'cancelled' if cancelled else (
            (error_diagnostics or {}).get('error_code') or error_type or 'unobserved_exception_type')
    elif pipeline_outcome is not None:
        state = pipeline_outcome.get('status')
        if state == 'verified' and answer_observed:
            group, outcome, stage, reason = 'success', 'internally_verified', None, None
        elif state == 'verified':
            outcome, reason = 'incomplete_record', 'final_answer_not_recorded'
        elif state == 'schema_validation_failed':
            group, outcome, reason = 'failure', 'schema_validation_failed', 'schema_validation_failed'
        elif state == 'incomplete':
            group, outcome = 'failure', 'incomplete_answer'
            reason = pipeline_outcome.get('stop_reason') or 'incomplete_verification'
            if not pipeline_outcome.get('evidence') and not pipeline_outcome.get('claims'):
                stage = 'retrieval_or_research'
            elif pipeline_outcome.get('issues') or pipeline_outcome.get('missing_answerable'):
                stage = 'verification'
            else:
                stage = 'finalization'
    return {'schema_version': 1, 'group': group, 'outcome': outcome,
            'failure_stage': stage, 'reason': reason,
            'classification_source': 'execution_and_internal_verifier',
            'provider_diagnostics': error_diagnostics,
            'answer_correctness_label': None, 'root_cause_label': None}


def classify_run(run):
    outcome, answer, error, diagnostics, role = None, False, None, None, None
    for event in run['events']:
        payload = event['payload']
        if event['kind'] == 'rag.request':
            role = payload.get('role')
        elif event['kind'] == 'rag.outcome':
            outcome = payload
        elif event['kind'] == 'answer.final':
            answer = True
        elif event['kind'] == 'run.error':
            error = payload.get('error_type')
            diagnostics = payload.get('diagnostics')
    return classify(run['status'], pipeline_outcome=outcome, answer_observed=answer,
                    error_type=error, error_diagnostics=diagnostics, last_role=role)
