"""Validated local manifests and content-addressed evidence."""
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


PDF_A_ANSWERS = {'yes', 'no', '0', '1', '2', '3', '4', '5'}


class Prediction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    answer: str = ''
    element_ids: list[str] = Field(default_factory=list)


def validate_prediction(case, payload, allowed_ids=None):
    prediction = Prediction.model_validate(payload)
    required = 'element_ids' if case['task'] in {'B', 'C'} else 'answer'
    if required not in prediction.model_fields_set:
        raise ValueError('Required task-specific prediction missing')
    ids = prediction.element_ids
    if any(not ident.strip() for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid or duplicate element IDs')
    if allowed_ids is not None and not set(ids) <= allowed_ids:
        raise ValueError('Element IDs outside input catalog')
    if required == 'answer' and ids:
        raise ValueError('Scalar task received element answers')
    return prediction


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(root, relative):
    root = Path(root).resolve()
    if Path(relative).is_absolute():
        raise ValueError('Benchmark assets must be relative paths')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Benchmark asset path escapes dataset directory')
    return path


class Element(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    text: str
    bbox: list[float] = Field(min_length=4, max_length=4)


class Page(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    page_number: int = Field(strict=True, ge=1)
    image: str = Field(min_length=1)
    image_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    text: str = ''
    elements: list[Element] = Field(default_factory=list)


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    dataset: str
    split: str
    task: str
    group: str
    question: str = Field(min_length=1)
    answers: list[str]
    gold_element_ids: list[str]
    scope: list[str] = Field(min_length=1)
    gold_pages: list[str]


def load_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or data.get('dataset') not in {'chartqa', 'pdfvqa'}:
        raise ValueError('Unsupported multimodal manifest')
    pages = [Page.model_validate(p).model_dump() for p in data['pages']]
    cases = [Case.model_validate(c).model_dump() for c in data['cases']]
    page_map = {p['id']: p for p in pages}
    if not pages or not cases or len(page_map) != len(pages) or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Empty or duplicate benchmark IDs')
    if len({(p['source'], p['page_number']) for p in pages}) != len(pages):
        raise ValueError('Duplicate benchmark page location')
    for page in pages:
        if sha256(inside(path.parent, page['image'])) != page['image_sha256']:
            raise ValueError('Benchmark image checksum mismatch')
        if len({e['id'] for e in page['elements']}) != len(page['elements']):
            raise ValueError('Duplicate element IDs on page')
    for case in cases:
        if case['dataset'] != data['dataset'] or case['split'] != data['split'] or not case['question'].strip():
            raise ValueError('Benchmark dataset/split mismatch')
        if case['task'] not in ({'chartqa'} if data['dataset'] == 'chartqa' else {'A', 'B', 'C'}):
            raise ValueError('Invalid benchmark task')
        if not set(case['scope']) <= page_map.keys() or not set(case['gold_pages']) <= set(case['scope']):
            raise ValueError('Gold/context pages missing from manifest')
        if case['task'] in {'chartqa', 'A'} and (not case['answers'] or not all(a.strip() for a in case['answers'])):
            raise ValueError('Unlabelled questions cannot be scored')
        if case['task'] == 'A' and (len(case['answers']) != 1 or case['answers'][0] not in PDF_A_ANSWERS):
            raise ValueError('Invalid PDFVQA A fixed answer')
        elements = {e['id'] for p in case['scope'] for e in page_map[p]['elements']}
        if not set(case['gold_element_ids']) <= elements:
            raise ValueError('Gold element IDs missing from input document')
    data.update(pages=pages, cases=cases, _root=path.parent, _manifest_sha256=sha256(path))
    return data
