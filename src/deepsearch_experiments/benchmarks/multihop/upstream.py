"""Pinned author assets; download data only, never execute upstream scripts."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from deepsearch_experiments.benchmarks.downloads import fetch
from deepsearch_agent.adapters.persistence.local_files import atomic_write


IRCOT_REVISION = '3c1820f698eea5eeddb4fba3c56b64c961e063e4'
LOGICRAG_REVISION = '72a298c95ffd75890c7f9c28fd1c094201f669a7'
REPOSITORIES = {
    'ircot': ('StonyBrookNLP/ircot', IRCOT_REVISION),
    'logicrag': ('chensyCN/LogicRAG', LOGICRAG_REVISION),
}
DATASETS = ('musique', '2wikimultihopqa')


def _acquire(output, assets, lock_name, *, allow_network=False, proxy=None):
    if not allow_network:
        raise ValueError('Downloading author assets requires --allow-network')
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Proxy must be an HTTP(S) URL without credentials')
    output = Path(output)
    with httpx.Client(timeout=60, proxy=proxy, follow_redirects=False) as client:
        records = []
        for project, path, max_bytes in assets:
            repo, revision = REPOSITORIES[project]
            url = f'https://raw.githubusercontent.com/{repo}/{revision}/{path}'
            raw = fetch(client, url, limit=max_bytes)
            local = Path(project) / path
            target = output / local
            if target.exists() and target.read_bytes() != raw:
                raise ValueError(f'Existing author asset differs: {local}; choose a new output')
            atomic_write(target, raw)
            records.append({'repository': repo, 'revision': revision, 'upstream_path': path,
                            'url': url, 'local_path': local.as_posix(), 'size_bytes': len(raw),
                            'sha256': hashlib.sha256(raw).hexdigest()})
    lock = {'schema_version': 1, 'assets': records, 'executed_upstream_code': False}
    target = output / lock_name
    payload = json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if target.exists() and target.read_bytes() != payload:
        raise ValueError('Asset lock differs; choose a new output')
    atomic_write(target, payload)
    return lock


def fetch_reference_assets(output, *, allow_network=False, proxy=None):
    assets = []
    for project in REPOSITORIES:
        assets.extend([(project, 'README.md', 512000), (project, 'LICENSE', 128000)])
    for dataset in DATASETS:
        assets.append(('ircot', f'prompts/{dataset}/gold_with_1_distractors_context_cot_qa_codex.txt', 256000))
        assets.append(('ircot', f'base_configs/ircot_qa_codex_{dataset}.jsonnet', 256000))
    assets.extend([
        ('ircot', 'commaqa/inference/ircot.py', 512000),
        ('ircot', 'commaqa/inference/prompt_reader.py', 256000),
        ('ircot', 'download/processed_data.sh', 128000),
        ('logicrag', 'src/models/logic_rag.py', 256000),
    ])
    return _acquire(output, assets, 'upstream.lock.json', allow_network=allow_network, proxy=proxy)


def fetch_logicrag_dataset(output, dataset, *, allow_network=False, proxy=None):
    if dataset not in DATASETS:
        raise ValueError('Unsupported author dataset')
    assets = [('logicrag', f'dataset/{dataset}.json', 20 * 1024 * 1024),
              ('logicrag', f'dataset/{dataset}_corpus.json', 12 * 1024 * 1024)]
    return _acquire(output, assets, f'logicrag-{dataset}.lock.json', allow_network=allow_network, proxy=proxy)
