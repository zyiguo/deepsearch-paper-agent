"""Versioned content fingerprints and provenance-preserving evidence identities."""
import hashlib
import json
import re
import unicodedata


def digest(prefix: str, value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return prefix + hashlib.sha256(encoded).hexdigest()


def text_content_id(text: str) -> str:
    normalized = unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n'))
    return digest('cnt_', ['text-v1', normalized])


def image_content_id(canonical_png: bytes) -> str:
    return 'cnt_' + hashlib.sha256(b'image-png-v1\0' + canonical_png).hexdigest()


def source_identity(source: str) -> str:
    match = re.match(r'^(pdf|image)://([a-f0-9]{64})(?:/|$)', source)
    if match:
        return f'{match[1]}://{match[2]}'
    match = re.match(r'^(attachment://[^/]+/[a-f0-9]{64})(?:/|$)', source)
    return match[1] if match else source


def identities(chunk):
    if type(chunk.identity_version) is not int or chunk.identity_version != 1:
        raise ValueError('不支持的片段 ID 版本')
    source = source_identity(chunk.source_path)
    end = chunk.page_end if chunk.page_end is not None else chunk.page_number
    if type(chunk.page_number) is not int or type(end) is not int or end - chunk.page_number > 1000:
        raise ValueError('片段 ID 页码范围无效')
    assets = [digest('ast_', ['page-v1', source, page]) for page in range(chunk.page_number, end + 1)]
    content = chunk.content_id if chunk.evidence_kind == 'image' else text_content_id(chunk.text)
    if content and not re.fullmatch(r'cnt_[a-f0-9]{64}', content):
        raise ValueError('内容 ID 无效')
    location = {'page': chunk.page_number, 'page_end': end}
    if chunk.page_spans:
        location['spans'] = [{'page': p['page_number'], 'start': p['start'], 'end': p['end']} for p in chunk.page_spans]
    else:
        location['index'] = chunk.chunk_index
    chunk_id = digest('chk_', ['chunk-v1', source, chunk.evidence_kind, location, content])
    return content, chunk_id, assets


def fingerprint(value):
    """Canonical SHA256 compatible with persisted trace/corpus identities."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()
