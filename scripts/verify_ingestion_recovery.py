"""Real process-exit tests with a synthetic, durably counted paid provider."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.knowledge.image_index import ImageVectorIndex
from deepsearch_agent.knowledge.index_service import IndexService
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.workspace.documents import DocumentLibrary, ExtractedPages
from deepsearch_agent.workspace.multimodal import PreparedDocument


CONTENT = b'isolated synthetic ingestion recovery fixture'
IDENT = hashlib.sha256(CONTENT).hexdigest()


def counted(root, stage):
    with sqlite3.connect(root / 'provider.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests(stage TEXT NOT NULL)')
        db.execute('INSERT INTO requests VALUES (?)', (stage,))


def counts(root):
    if not (root / 'provider.sqlite3').exists():
        return {}
    with sqlite3.connect(root / 'provider.sqlite3') as db:
        return dict(db.execute('SELECT stage,count(*) FROM requests GROUP BY stage'))


def worker(root, *, fault_stage='', fault_step='', confirm=False):
    class Embed:
        def embed(self, texts):
            counted(root, 'text_vectors')
            return [[.3, .7] for _ in texts]
    class ImageEmbed:
        def embed_images(self, paths):
            counted(root, 'image_vectors')
            return [[.7, .3] for _ in paths]
    class Processor:
        analyzer = SimpleNamespace(identity='synthetic-process-fault-v1')
        def prepare(self, pages, source, ident, render):
            counted(root, 'analysis')
            return PreparedDocument(pages, [TextChunk(pages[0], source, 1, 0)], [])
    kb = KnowledgeBase(ExtractedPages([]), embedding_client=Embed(), embedding_id='synthetic-process-v1')
    kb.image_index = ImageVectorIndex('synthetic-image-v1', ImageEmbed())
    index = IndexService(kb, root / 'index.json')
    if index.index_path.exists():
        index.restore()
    docs = DocumentLibrary(root / 'documents', index, processor=Processor())
    try:
        if IDENT not in docs.records:
            docs.path(IDENT, '.pdf').write_bytes(CONTENT)
            docs.records[IDENT] = dict(id=IDENT, title='synthetic.pdf', source_path=f'pdf://{IDENT}/synthetic.pdf',
                                       page_count=0, chunk_count=0, status='failed', error=None, warnings=[])
            docs.persist(docs.records[IDENT])
        def extract(operation, ident, target, page=None):
            counted(root, 'parse')
            target.write_text('["Synthetic verified page."]', encoding='utf-8')
        docs.worker = extract
        image = root / 'page.png'
        image.write_bytes(b'synthetic image')
        docs.render_page = lambda *a, **k: image
        def crash(stage, step):
            if (stage, step) == (fault_stage, fault_step):
                os._exit(73)
        docs.fault = crash
        checked = docs.consistency(IDENT, repair=True)
        if not checked['repaired']:
            docs.process(IDENT, confirm_unknown=confirm)
        record = docs.view(IDENT)
        print(json.dumps({'status': record['status'], 'unknown': record['retry_requires_confirmation'],
                          'counts': counts(root), 'consistency': docs.consistency(IDENT)['status']}))
    finally:
        docs.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--fault-stage', default='')
    parser.add_argument('--fault-step', default='')
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker(args.output, fault_stage=args.fault_stage, fault_step=args.fault_step, confirm=args.confirm)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    cases = [(stage, step) for step in ('analysis', 'text_vectors', 'image_vectors')
             for stage in ('after_dispatch', 'after_effect', 'after_artifact')]
    cases.append(('after_publish', 'publication'))
    results = []
    for number, (stage, step) in enumerate(cases):
        root = args.output / f'case-{number:02d}'
        root.mkdir()
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', '--output', str(root.resolve())]
        first = subprocess.run([*command, '--fault-stage', stage, '--fault-step', step], capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert first.returncode == 73, first.stderr
        before = counts(root)
        resumed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert resumed.returncode == 0, resumed.stderr
        recovery = json.loads(resumed.stdout)
        needs_consent = stage in {'after_dispatch', 'after_effect'}
        assert recovery['unknown'] == needs_consent, recovery
        if needs_consent:
            assert recovery['status'] == 'failed' and recovery['counts'] == before
            confirmed = subprocess.run([*command, '--confirm'], capture_output=True, text=True, encoding='utf-8', timeout=30)
            assert confirmed.returncode == 0, confirmed.stderr
            final = json.loads(confirmed.stdout)
        else:
            final = recovery
        assert final['status'] == 'ready' and final['consistency'] == 'consistent', final
        expected = {s: 1 for s in ('parse', 'analysis', 'text_vectors', 'image_vectors')}
        if stage == 'after_effect':
            expected[step] = 2  # a deliberate, consented retry of an unknown charge
        assert final['counts'] == expected, final
        results.append(dict(stage=stage, step=step, crash_exit=first.returncode, before=before,
                            recovery=recovery, explicitly_confirmed=needs_consent, final=final, passed=True))
    report = dict(schema_version=1, kind='synthetic_provider_process_faults', cases=results,
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), passed=len(results))
    with (args.output / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'passed': len(results), 'report': str(args.output / 'report.json')}))


if __name__ == '__main__':
    main()
