"""Opt-in persistent trace settings, without importing a trace database implementation."""
import os
from pathlib import Path
from dotenv import dotenv_values


def tracing_enabled(path):
    values = dict(os.environ)
    path = Path(path)
    if path.is_file():
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
    value = values.get('TRACING_ENABLED', 'false').strip().lower()
    if value not in {'true', 'false'}:
        raise ValueError('TRACING_ENABLED must be true or false')
    return value == 'true'
