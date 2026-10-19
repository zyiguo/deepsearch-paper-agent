"""Index-lock boundary shared by Web and the application service."""
def snapshot_answer_service(state):
    snapshot = getattr(state.answer_service, 'snapshot', None)
    return snapshot(state.kb) if callable(snapshot) else state.answer_service
