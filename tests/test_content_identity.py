from dataclasses import asdict, replace
import json

import pytest

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.identity import image_content_id


def chunk(text='same', source='pdf://' + 'a' * 64 + '/one.pdf', **kwargs):
    return TextChunk(text, source, kwargs.pop('page_number', 1), kwargs.pop('chunk_index', 0), **kwargs)


def test_same_content_different_occurrences_and_renames():
    one = chunk()
    renamed = chunk(source='pdf://' + 'a' * 64 + '/renamed.pdf')
    other = chunk(source='pdf://' + 'b' * 64 + '/two.pdf')
    assert one.content_id == other.content_id
    assert one.chunk_id != other.chunk_id
    assert one.chunk_id == renamed.chunk_id
    assert chunk('same\r\nline').content_id == chunk('same\nline').content_id
    assert chunk('a b').content_id != chunk('ab').content_id


def test_visual_representations_share_asset_not_chunk_identity():
    figure = chunk(evidence_kind='figure', visual_pages=[1])
    ocr = chunk(evidence_kind='ocr', visual_pages=[1])
    image = chunk(evidence_kind='image', visual_pages=[1], content_id=image_content_id(b'pixels'))
    assert figure.asset_ids == ocr.asset_ids == image.asset_ids
    assert len({figure.chunk_id, ocr.chunk_id, image.chunk_id}) == 3
    assert chunk(evidence_kind='image', visual_pages=[1], content_id=image_content_id(b'new pixels')).chunk_id != image.chunk_id
    assert chunk(evidence_kind='image', visual_pages=[1]).content_id == ''


def test_snapshot_identity_validation_and_prompt_excerpt_preserve_identity():
    original = chunk('a long body')
    shortened = replace(original, text='a long')
    assert shortened.chunk_id == original.chunk_id
    assert TextChunk(**asdict(original)).chunk_id == original.chunk_id
    with pytest.raises(ValueError):
        TextChunk(**{**asdict(original), 'text': 'tampered'}).validate_structure()


def test_source_reference_and_legacy_snapshot_upgrade(tmp_path):
    from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
    from deepsearch_agent.core.answers import chunk_to_source_reference
    class Embedding:
        def embed(self, texts):
            return [[1., 0.] for _ in texts]
    kb = KnowledgeBase(None, embedding_client=Embedding(), embedding_id='test')
    original = chunk()
    kb.ingest_chunks([original])
    path = tmp_path / 'index.json'
    kb.save(path)
    payload = json.loads(path.read_text())
    assert payload['schema_version'] == 4
    assert payload['entries'][0]['chunk']['chunk_id'] == original.chunk_id
    payload['schema_version'] = 2
    for key in ('chunk_id', 'content_id', 'asset_ids', 'identity_version'):
        payload['entries'][0]['chunk'].pop(key)
    path.write_text(json.dumps(payload), encoding='utf-8')
    kb.load(path)
    assert kb.chunks[0].chunk_id == original.chunk_id
    assert chunk_to_source_reference(kb.chunks[0]).chunk_id == original.chunk_id


def test_fusion_uses_occurrence_identity_without_merging_different_sources():
    from deepsearch_agent.knowledge.image_index import fuse_rankings
    first = chunk()
    renamed = chunk(source='pdf://' + 'a' * 64 + '/renamed.pdf')
    other = chunk(source='pdf://' + 'b' * 64 + '/other.pdf')
    results = fuse_rankings([[first, other], [renamed]], 3)
    assert [c.chunk_id for c in results] == [first.chunk_id, other.chunk_id]


def test_legacy_image_hydration_reuses_existing_vectors(tmp_path):
    from deepsearch_agent.knowledge.image_index import ImageVectorIndex
    old = chunk(evidence_kind='image', visual_pages=[1])
    index = ImageVectorIndex('model')
    index.chunks, index.vectors = [old], [[1., 0.]]
    image = tmp_path / 'page.png'
    image.write_bytes(b'canonical image bytes')
    assert index.hydrate_legacy_identities(lambda source, page: image) == 1
    assert old.content_id == image_content_id(image.read_bytes())
    assert index.vectors == [[1., 0.]]
    assert index.hydrate_legacy_identities(lambda source, page: image) == 0
