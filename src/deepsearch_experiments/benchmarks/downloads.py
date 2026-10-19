"""Bounded public-data downloads; immutable normalized PNGs and provenance."""
import hashlib
import json
from pathlib import Path
import random
import time
from urllib.parse import urlsplit, quote

import httpx

from deepsearch_experiments.benchmarks.datasets import chartqa_cases, pdfvqa_page_cases
from deepsearch_agent.workspace.image_assets import normalize_image
from deepsearch_agent.adapters.persistence.local_files import atomic_write

CHARTQA_REVISION = '044eabfc306abfe9340c5741f0093aefc5973d06'
ALLOWED_HOSTS = {'raw.githubusercontent.com', 'datasets-server.huggingface.co', 'huggingface.co'}


def fetch(client, url, limit=16 * 1024 * 1024):
    target = urlsplit(url)
    if target.scheme != 'https' or target.hostname not in ALLOWED_HOSTS or target.username or target.password:
        raise ValueError('Dataset download host not allowed')
    for attempt in range(3):
        try:
            with client.stream('GET', url) as response:
                response.raise_for_status()
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > limit:
                        raise ValueError('Public dataset file exceeds download limit')
                return bytes(content)
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            retryable = isinstance(error, httpx.RequestError) or error.response.status_code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 2:
                raise
            time.sleep(attempt + 1)


def store_image(output, content):
    png, _, _ = normalize_image(content)
    ident = hashlib.sha256(png).hexdigest()
    relative = f'pages/{ident}.png'
    atomic_write(Path(output).parent / relative, png)
    return relative, ident


def write_manifest(output, dataset, split, pages, cases, provenance):
    payload = dict(schema_version=1, dataset=dataset, split=split, pages=pages, cases=cases, provenance=provenance)
    atomic_write(Path(output), json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
    return payload


def download_chartqa(output, *, split='val', limit=100, seed=42, revision=CHARTQA_REVISION):
    if split not in {'train', 'val', 'test'} or not 1 <= limit <= 2500:
        raise ValueError('Invalid ChartQA split or sample size')
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('ChartQA revision must be a pinned commit SHA')
    base = f'https://raw.githubusercontent.com/vis-nlp/ChartQA/{revision}/ChartQA%20Dataset/{split}'
    cases, hashes = [], {}
    rng = random.Random(seed)
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for group, count in [('human', (limit+1)//2), ('augmented', limit//2)]:
            raw = fetch(client, f'{base}/{split}_{group}.json')
            hashes[group] = hashlib.sha256(raw).hexdigest()
            rows = json.loads(raw)
            all_cases = chartqa_cases(rows, split, group)
            cases.extend(all_cases[i] for i in sorted(rng.sample(range(len(rows)), min(count, len(rows)))))
        if len(cases) != limit:
            raise ValueError('Not enough ChartQA questions for balanced sample')
        pages = []
        for i, key in enumerate(sorted({c['scope'][0] for c in cases}), 1):
            filename = key.rsplit('/', 1)[1]
            image, fingerprint = store_image(output, fetch(client, f'{base}/png/{quote(filename)}'))
            pages.append(dict(id=key, source=f'benchmark://chartqa/{split}/{filename}', page_number=1,
                              image=image, image_sha256=fingerprint, text='', elements=[]))
            if i % 10 == 0:
                print(f'ChartQA images {i}', flush=True)
    return write_manifest(output, 'chartqa', split, pages, cases,
                          dict(source='https://github.com/vis-nlp/ChartQA', revision=revision, seed=seed,
                               sampling='seeded balanced human/augmented question sample', label_sha256=hashes,
                               status='public-dataset-subset', full_benchmark=False))


def download_pdfvqa(output, *, split='validation', limit=100, seed=42):
    if split not in {'train', 'validation', 'test'} or not 1 <= limit <= 2500:
        raise ValueError('Invalid PDFVQA split or sample size')
    base = 'https://datasets-server.huggingface.co/rows'
    def row_url(offset):
        return f'{base}?dataset=gigant/pdfvqa&config=default&split={split}&offset={offset}&length=1'
    rng = random.Random(seed)
    counts = {'A': (limit+1)//2, 'B': limit//2}
    selected, pages, skipped, scanned, raw_hashes = [], [], 0, 0, {}
    revision = None
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        head = json.loads(fetch(client, row_url(0)))
        for index in rng.sample(range(head['num_rows_total']), head['num_rows_total']):
            raw = fetch(client, row_url(index))
            row = json.loads(raw)['rows'][0]['row']
            raw_hashes[str(index)] = hashlib.sha256(raw).hexdigest()
            scanned += 1
            candidates, excluded = pdfvqa_page_cases(row, index, split)
            skipped += excluded
            chosen = []
            for task in ('A', 'B'):
                choices = [c for c in candidates if c['task'] == task]
                if counts[task] and choices:
                    chosen.append(rng.choice(choices))
                    counts[task] -= 1
            if chosen:
                src = row['page']['src']
                parts = urlsplit(src).path.split('/')
                row_revision = parts[parts.index('--') + 1]
                if revision is not None and row_revision != revision:
                    raise ValueError('PDFVQA mirror changed during sampling')
                revision = row_revision
                image, fingerprint = store_image(output, fetch(client, src))
                key = f'pdfvqa/{split}/page-{index}'
                elements = [dict(id=str(ident), text=text or '', bbox=box)
                            for ident, text, box in zip(row['object_ids'], row['texts'], row['bboxes'], strict=True)]
                pages.append(dict(id=key, source=f'benchmark://pdfvqa/{split}/page-{index}', page_number=1,
                                  image=image, image_sha256=fingerprint, text='\n'.join(e['text'] for e in elements), elements=elements))
                selected.extend(chosen)
            if scanned % 10 == 0:
                print(f'PDFVQA scanned {scanned} pages, selected {len(selected)} questions', flush=True)
            if not any(counts.values()):
                break
    if any(counts.values()):
        raise ValueError('Not enough labelled unambiguous PDFVQA A/B questions')
    return write_manifest(output, 'pdfvqa', split, pages, selected,
                          dict(source='https://github.com/adlnlp/pdfvqa', mirror='https://huggingface.co/datasets/gigant/pdfvqa',
                               revision=revision, seed=seed, sampling='seeded page sample; at most one question per task per page',
                               status='public-mirror-subset', full_benchmark=False, tasks=['A', 'B'],
                               limitations=['page-only mirror, no Task C or document IDs',
                                            'B labels mapped from OCR text only on unique match; not official ID release'],
                               bbox_format='xywh', skipped_ambiguous_or_invalid_labels=skipped, scanned_pages=scanned,
                               raw_row_sha256=raw_hashes))
