"""Persisted request identity and safe execution diagnostics; no HTTP/storage implementation."""
import re
from dataclasses import asdict, is_dataclass

from deepsearch_agent.core.identity import fingerprint
from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.models import ModelServiceError


def submission_id(session_id, key):
    if key is None:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key):
        raise ApplicationError(422, 'Idempotency-Key 须为 1–128 位字母、数字或 _ . : -')
    return fingerprint(['submission-v1', session_id, key])[:32]


def context_identity(state, manager, session_id):
    context, history = manager.context(session_id)
    skills = getattr(state, 'skills', None)
    skill_hashes = []
    if skills is not None:
        for path in sorted(skills.root.glob('*/SKILL.md'))[:128]:
            # A changed skill must not silently change a resumed agent's instructions.
            if path.resolve().is_relative_to(skills.root) and path.is_file() and path.stat().st_size <= 32768:
                skill_hashes.append([path.parent.name, fingerprint(path.read_text(encoding='utf-8'))])
    service = state.answer_service
    config = getattr(state, 'run_identity', None)
    if config is None:
        settings = getattr(getattr(service, 'client', None), 'settings', None)
        config = fingerprint(asdict(settings) if is_dataclass(settings) else {
            'context_window': getattr(service, 'context_window', None),
            'max_output_tokens': getattr(service, 'max_output_tokens', None)})
    wiki = getattr(service, 'wiki_store', None)
    wiki_hashes = []
    if wiki is not None:
        for path in sorted(wiki.root.glob('*')):
            if path.is_file() and path.suffix in {'.md', '.json'}:
                wiki_hashes.append([path.name, fingerprint(path.read_text(encoding='utf-8'))])
    return fingerprint(['resume-v1', config, context, history, skill_hashes, wiki_hashes])


def error_code(error):
    if isinstance(error, ModelServiceError):
        return error.diagnostics().get('error_code') or 'model_service_error'
    if isinstance(error, ApplicationError) and error.status_code == 503:
        return 'service_or_persistence_unavailable'
    return 'execution_error'
