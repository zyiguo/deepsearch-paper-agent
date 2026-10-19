"""Deterministic source identity across the modular application and offline tools."""
import hashlib
from pathlib import Path


def source_fingerprint(*, excluded=()):
    root = Path(__file__).resolve().parents[1]
    files = sorted(p for name in ('deepsearch_agent', 'deepsearch_tracing', 'deepsearch_experiments', 'search_policy')
                   for p in (root / name).rglob('*.py') if p.name not in excluded)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
