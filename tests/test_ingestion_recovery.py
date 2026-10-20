from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace

import pytest

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.knowledge.image_index import ImageVectorIndex
from deepsearch_agent.knowledge.index_service import IndexService
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.workspace.documents import DocumentError, DocumentLibrary, ExtractedPages
from deepsearch_agent.workspace.ingestion import IngestionBlocked, IngestionJournal
from deepsearch_agent.workspace.multimodal import PreparedDocument
from test_tool_execution_ledger import PowerLoss


IDENT = hashlib.sha256(b'synthetic-document').hexdigest()


def library(root, calls, *, images=False, vision=False, identity='synthetic-v1'):
    class Embed:
        def embed(self, texts):
            calls.append('text_vectors')
            return [[1., 0.] for _ in texts]
    class ImageEmbed:
        def embed_images(self, paths):
            calls.append('image_vectors')
            return [[0., 1.] for _ in paths]
    class Processor:
        analyzer = SimpleNamespace(identity='synthetic-vision')
        def prepare(self, pages, source, ident, render):
            calls.append('analysis')
            return PreparedDocument(pages, [TextChunk(pages[0], source, 1, 0)], [])
    kb = KnowledgeBase(ExtractedPages([]), embedding_client=Embed(), embedding_id=identity)
    if images:
        kb.image_index = ImageVectorIndex('synthetic-image-v1', ImageEmbed())
    index = IndexService(kb, root / 'index.json')
    if index.index_path.exists():
        index.restore()
    documents = DocumentLibrary(root / 'documents', index, processor=Processor() if vision else None)
    if IDENT not in documents.records:
        record = dict(id=IDENT, title='synthetic.pdf', source_path=f'pdf://{IDENT}/synthetic.pdf',
                      status='failed', page_count=0, error=None, warnings=[])
        documents.path(IDENT, '.pdf').write_bytes(b'synthetic-document')
        documents.records[IDENT] = record
        documents.persist(record)
    def worker(operation, ident, target, page=None):
        calls.append('parse')
        target.write_text(json.dumps(['A verified local document.']), encoding='utf-8')
    documents.worker = worker
    image = root / 'page.png'
    image.write_bytes(b'synthetic image bytes')
    documents.render_page = lambda *a, **k: image
    return documents


@pytest.mark.parametrize('step', ['analysis', 'text_vectors', 'image_vectors'])
@pytest.mark.parametrize('stage', ['after_dispatch', 'after_effect', 'after_artifact'])
def test_paid_step_crash_requires_consent_or_reuses_committed_artifact(tmp_path, step, stage):
    calls = []
    first = library(tmp_path, calls, images=True, vision=True)
    def fault(actual_stage, actual_step):
        if (actual_stage, actual_step) == (stage, step):
            raise PowerLoss()
    first.fault = fault
    with pytest.raises(PowerLoss):
        first.process(IDENT)
    first.close()
    before = list(calls)
    second = library(tmp_path, calls, images=True, vision=True)
    try:
        second.process(IDENT)
        if stage == 'after_artifact':
            assert second.view(IDENT)['status'] == 'ready'
            assert calls.count(step) == 1
        else:
            assert second.view(IDENT)['status'] == 'failed'
            assert second.view(IDENT)['retry_requires_confirmation']
            assert calls == before
            with pytest.raises(DocumentError, match='结果未知'):
                second.retry(IDENT)
            second.process(IDENT, confirm_unknown=True)
            assert second.view(IDENT)['status'] == 'ready'
            assert calls.count(step) == (1 if stage == 'after_dispatch' else 2)
            assert second.journal(IDENT).read(step)['confirmed_unknown_attempts'] == [2]
        assert calls.count('parse') == 1
        for prior in ['analysis', 'text_vectors', 'image_vectors'][:['analysis', 'text_vectors', 'image_vectors'].index(step)]:
            assert calls.count(prior) == 1
    finally:
        second.close()


def test_publish_before_ready_crash_repaired_without_paid_calls_or_reparsing(tmp_path):
    calls = []
    first = library(tmp_path, calls, images=True, vision=True)
    first.fault = lambda stage, step: (_ for _ in ()).throw(PowerLoss()) if stage == 'after_publish' else None
    with pytest.raises(PowerLoss):
        first.process(IDENT)
    first.close()
    before_index = (tmp_path / 'index.json').read_bytes()
    second = library(tmp_path, calls, images=True, vision=True)
    try:
        original_bytes = second.path(IDENT, '.pdf').read_bytes()
        assert second.view(IDENT)['status'] == 'failed'
        checked = second.consistency(IDENT)
        assert checked['status'] == 'consistent' and not checked['repaired']
        assert second.view(IDENT)['status'] == 'failed'  # read-only inspection
        assert second.retry(IDENT)['status'] == 'ready'
        assert calls == ['parse', 'analysis', 'text_vectors', 'image_vectors']
        assert (tmp_path / 'index.json').read_bytes() == before_index
        assert second.path(IDENT, '.pdf').read_bytes() == original_bytes
    finally:
        second.close()


def test_failed_publication_reuses_vectors_and_preserves_other_document(tmp_path):
    calls = []
    docs = library(tmp_path, calls)
    original_publish = docs.index.publish
    docs.index.publish = lambda _: (_ for _ in ()).throw(OSError('synthetic disk full'))
    docs.process(IDENT)
    assert docs.view(IDENT)['status'] == 'failed' and docs.index.kb.chunks == []
    docs.index.publish = original_publish
    docs.index.kb.ingest_chunks([TextChunk('other document', 'other.pdf', 1, 0)])
    docs.process(IDENT)
    try:
        assert docs.view(IDENT)['status'] == 'ready'
        assert calls == ['parse', 'text_vectors', 'text_vectors']  # one separate document embedding
        assert len(docs.index.kb.chunks) == 2
        assert docs.consistency(IDENT)['status'] == 'consistent'
    finally:
        docs.close()


def test_consistency_refuses_repair_when_published_content_or_original_is_missing(tmp_path):
    docs = library(tmp_path, [])
    try:
        docs.process(IDENT)
        docs.update(IDENT, status='failed')
        docs.path(IDENT, '.pdf').unlink()
        assert not docs.consistency(IDENT, repair=True)['repaired']
        docs.path(IDENT, '.pdf').write_bytes(b'synthetic-document')
        docs.path(IDENT, '.pages.json').write_text('["modified extracted content"]', encoding='utf-8')
        assert not docs.consistency(IDENT, repair=True)['repaired']
        docs.index.kb = docs.index.kb.restrict_sources(set())
        assert docs.consistency(IDENT, repair=True)['status'] == 'index_or_artifact_mismatch'
        assert docs.view(IDENT)['status'] == 'failed'
    finally:
        docs.close()


def test_image_backfill_interruption_repairs_only_image_publication(tmp_path):
    calls = []
    first = library(tmp_path, calls)
    first.process(IDENT)
    first.close()
    second = library(tmp_path, calls, images=True)
    second.fault = lambda stage, step: (_ for _ in ()).throw(PowerLoss()) if stage == 'after_publish' else None
    try:
        with pytest.raises(PowerLoss):
            second.process(IDENT, images_only=True)
    finally:
        second.close()
    third = library(tmp_path, calls, images=True)
    try:
        assert third.consistency(IDENT, repair=True)['repaired']
        assert third.view(IDENT)['image_indexed_pages'] == 1
        assert third.view(IDENT)['analysis_mode'] == 'text'
        assert calls == ['parse', 'text_vectors', 'image_vectors']
    finally:
        third.close()


def test_generic_retry_continues_original_image_only_step(tmp_path):
    calls = []
    first = library(tmp_path, calls)
    first.process(IDENT)
    first.close()
    second = library(tmp_path, calls, images=True)
    second.fault = lambda stage, step: (_ for _ in ()).throw(PowerLoss()) if (stage, step) == ('after_effect', 'image_vectors') else None
    with pytest.raises(PowerLoss):
        second.process(IDENT, images_only=True)
    second.close()
    third = library(tmp_path, calls, images=True)
    with pytest.raises(DocumentError, match='结果未知'):
        third.retry(IDENT)
    third.retry(IDENT, confirm_unknown=True)
    third.close()  # waits for the background step
    assert third.view(IDENT)['status'] == 'ready'
    assert calls == ['parse', 'text_vectors', 'image_vectors', 'image_vectors']


def test_corrupt_or_missing_committed_artifact_never_regenerates_paid_result(tmp_path):
    calls = []
    job = IngestionJournal(tmp_path, {'document': 'synthetic', 'model': 'v1'})
    assert job.run('embedding', lambda: calls.append(1) or [1, 0], paid=True) == [1, 0]
    artifact = job._path('embedding', 'artifact')
    value = json.loads(artifact.read_text())
    value['value'] = [0, 1]
    artifact.write_text(json.dumps(value))
    with pytest.raises(IngestionBlocked, match='校验失败'):
        job.run('embedding', lambda: calls.append(2), paid=True)
    artifact.unlink()
    with pytest.raises(IngestionBlocked, match='产物丢失'):
        job.run('embedding', lambda: calls.append(3), paid=True)
    assert calls == [1]


def test_model_identity_uses_separate_artifacts_without_rewriting_previous(tmp_path):
    first = IngestionJournal(tmp_path, {'document': IDENT, 'model': 'v1'})
    first.run('embedding', lambda: [1, 0], paid=True)
    path = first._path('embedding', 'artifact')
    original = path.read_bytes()
    second = IngestionJournal(tmp_path, {'document': IDENT, 'model': 'v2'})
    assert second.run('embedding', lambda: [0, 1], paid=True) == [0, 1]
    assert first.root != second.root and path.read_bytes() == original
