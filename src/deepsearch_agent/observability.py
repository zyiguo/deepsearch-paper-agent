"""Optional application events; observers must never affect answering."""
import logging
from dataclasses import asdict
import hashlib
import json


def notify(observer, kind, payload):
    if observer is not None:
        try:
            observer(kind, payload)
        except Exception:
            logging.getLogger(__name__).warning('rag_observer_failed')


def chunk_record(chunk):
    value = asdict(chunk)
    identity = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return {'chunk_id': hashlib.sha256(identity.encode('utf-8')).hexdigest(), **value}
