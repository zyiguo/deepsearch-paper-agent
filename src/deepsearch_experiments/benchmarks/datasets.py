"""Explicit adapters for ChartQA and PDFVQA, never use eval or executable pickle."""
import ast
import csv
import io
import json
import pickle
from pathlib import Path

from deepsearch_experiments.benchmarks.metrics import normalized_text
from deepsearch_experiments.benchmarks.schema import PDF_A_ANSWERS


def primitive_pickle(content):
    class PrimitiveOnly(pickle.Unpickler):
        def find_class(self, module, name):
            raise ValueError('Refusing executable pickle; export trusted data to JSON first')
        def persistent_load(self, pid):
            raise ValueError('Refusing executable pickle references')
    return PrimitiveOnly(io.BytesIO(content)).load()


def read_data(path):
    path = Path(path)
    if path.suffix == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))
    if path.suffix in {'.pkl', '.pickle'}:
        return primitive_pickle(path.read_bytes())
    return json.loads(path.read_text(encoding='utf-8'))


def values(value):
    if isinstance(value, str):
        if value.strip().startswith('['):
            value = ast.literal_eval(value)
        else:
            return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    if value is None:
        return []
    return [str(value)]


def chartqa_cases(rows, split, group):
    cases = []
    for index, row in enumerate(rows):
        name = row['imgname']
        if not isinstance(name, str) or '/' in name or '\\' in name or not name.lower().endswith('.png'):
            raise ValueError('Invalid ChartQA image filename')
        page = f'chartqa/{split}/{name}'
        label = row.get('label')
        answers = [label] if isinstance(label, str) else values(label)
        if not answers or not all(a.strip() for a in answers):
            raise ValueError('ChartQA row has no answer')
        cases.append(dict(id=f'chartqa/{split}/{group}/{index}', dataset='chartqa', split=split,
                          task='chartqa', group=group, question=row['query'], answers=answers,
                          gold_element_ids=[], scope=[page], gold_pages=[page]))
    return cases


def pdfvqa_page_cases(row, index, split):
    fields = ('questions', 'answers', 'types', 'task types')
    if len({len(row[k]) for k in fields}) != 1:
        raise ValueError('Misaligned PDFVQA question labels')
    if not (len(row['object_ids']) == len(row['texts']) == len(row['bboxes'])):
        raise ValueError('Misaligned PDFVQA elements')
    page = f'pdfvqa/{split}/page-{index}'
    cases, skipped = [], 0
    for q, (question, answer, kind, task) in enumerate(zip(*(row[k] for k in fields), strict=True)):
        if task not in {'A', 'B'}:
            raise ValueError('Page mirror cannot reconstruct Task C documents')
        gold = []
        if answer is None or not str(answer).strip():
            skipped += 1
            continue
        if task == 'A':
            answer = {'true': 'yes', 'false': 'no'}.get(normalized_text(answer), normalized_text(answer))
            if answer not in PDF_A_ANSWERS:
                skipped += 1
                continue
        else:
            matches = [str(ident) for ident, text in zip(row['object_ids'], row['texts'], strict=True)
                       if normalized_text(text) == normalized_text(answer)]
            if len(matches) != 1:
                skipped += 1
                continue
            gold = matches
        cases.append(dict(id=f'pdfvqa/{split}/{index}/{q}', dataset='pdfvqa', split=split, task=task,
                          group=kind, question=question, answers=[str(answer)], gold_element_ids=gold,
                          scope=[page], gold_pages=[page]))
    return cases, skipped


def pdfvqa_document_cases(rows, layout, task, split):
    """Original CSV/JSON: pmcid, question, global_id; zero-based page_index for A/B."""
    pages, cases = {}, []
    for index, row in enumerate(rows):
        answers = values(row.get('answer'))
        if task == 'A':
            answers = [{'true': 'yes', 'false': 'no'}.get(normalized_text(a), normalized_text(a)) for a in answers]
            if len(answers) != 1 or answers[0] not in PDF_A_ANSWERS:
                raise ValueError('Unlabelled or invalid PDFVQA A fixed answer')
        elif row.get('global_id') is None or (isinstance(row['global_id'], str) and not row['global_id'].strip()):
            raise ValueError('Unlabelled PDFVQA CSV: ground-truth global_id required')
        doc = str(row['pmcid'])
        info = layout[doc]['pages']
        scope = sorted(info, key=int) if task == 'C' else [str(int(row['page_index']))]
        gold_ids = values(row.get('global_id'))
        if any(not ident.strip() for ident in gold_ids):
            raise ValueError('Invalid PDFVQA ground-truth element ID')
        gold_pages = []
        for number in scope:
            original = info.get(number, info.get(int(number)))
            if original is None:
                raise ValueError('PDFVQA input page missing')
            key = f'pdfvqa/{split}/{doc}/{int(number)+1}'
            objects = list(original['objects'].values())
            elements = [{'id': str(o['global_id']), 'text': o['text'] or '', 'bbox': o['bbox']} for o in objects]
            if set(gold_ids) & {e['id'] for e in elements}:
                gold_pages.append(key)
            pages[key] = dict(id=key, source=f'benchmark://pdfvqa/{split}/{doc}', page_number=int(number)+1,
                              image=original['name'], text='\n'.join(e['text'] for e in elements), elements=elements)
        keys = [f'pdfvqa/{split}/{doc}/{int(number)+1}' for number in scope]
        cases.append(dict(id=f'pdfvqa/{split}/{task}/{index}', dataset='pdfvqa', split=split, task=task,
                          group=row.get('question_type', task), question=row['question'], answers=answers,
                          gold_element_ids=gold_ids if task != 'A' else [], scope=keys,
                          gold_pages=gold_pages if task != 'A' else keys))
    return list(pages.values()), cases
