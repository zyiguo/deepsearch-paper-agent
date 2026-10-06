"""Optional application events; observers must never affect answering."""
import logging
from dataclasses import asdict


def notify(observer, kind, payload):
    if observer is not None:
        try:
            observer(kind, payload)
        except Exception:
            logging.getLogger(__name__).warning('rag_observer_failed')


def chunk_record(chunk):
    return asdict(chunk)
