import json
from types import SimpleNamespace

import pytest

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.multimodal_reranker import MultimodalReranker


def candidates(count=3):
    return [TextChunk(f'candidate {i}', 'paper.pdf', i + 1, 0,
                      evidence_kind='figure', visual_pages=[i + 1]) for i in range(count)]


class Visual:
    settings = SimpleNamespace(max_images=1, image_token_budget=1000,
                               llm=SimpleNamespace(context_window=16000, max_output_tokens=1024))
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
    def complete(self, prompt, evidence, **kwargs):
        self.calls.append((prompt, evidence))
        if self.fail_at == len(self.calls):
            raise RuntimeError('timeout')
        items = json.loads(prompt.split('\ndata: ', 1)[1])['candidates']
        return json.dumps({'scores': [{'chunk_id': c['chunk_id'], 'score': c['page'] * 20} for c in items]})


def test_batches_visual_descriptions_and_keeps_all_candidates():
    chunks = candidates()
    visual = Visual()
    reranker = MultimodalReranker(visual)
    assert reranker.rerank('q', chunks, 3) == list(reversed(chunks))
    assert len(visual.calls) == 3 and reranker.last_status == 'ok_multimodal'
    assert {c['chunk_id'] for _, evidence in visual.calls for c in evidence} == {c.chunk_id for c in chunks}


def test_mixed_text_images_and_shared_page_budget():
    chunks = candidates(1)
    chunks += [TextChunk('body', 'paper.pdf', 1, 1), TextChunk('OCR', 'paper.pdf', 1, 2, evidence_kind='ocr', visual_pages=[1])]
    visual = Visual()
    assert len(MultimodalReranker(visual).rerank('q', chunks, 3)) == 3
    assert len(visual.calls) == 1


def test_failure_in_late_batch_preserves_original_order():
    chunks = candidates()
    reranker = MultimodalReranker(Visual(fail_at=2))
    assert reranker.rerank('q', chunks, 2) == chunks[:2]
    assert reranker.last_status == 'fallback_multimodal'


@pytest.mark.parametrize('payload', [{'scores': []}, {'scores': [{'chunk_id': 'invented', 'score': 100}]},
                                    {'scores': [{'chunk_id': 'x', 'score': True}]}])
def test_invalid_ids_and_scores_fall_back(payload):
    class Bad(Visual):
        def complete(self, *args, **kwargs):
            return json.dumps(payload)
    chunks = candidates(2)
    assert MultimodalReranker(Bad()).rerank('q', chunks, 2) == chunks


def test_text_only_routes_to_existing_text_reranker():
    class Text:
        last_status = 'ok'
        def rerank(self, query, chunks, top_k):
            return list(reversed(chunks))[:top_k]
    chunks = [TextChunk('a', 'p', 1, 0), TextChunk('b', 'p', 1, 1)]
    reranker = MultimodalReranker(Visual(), text_reranker=Text())
    assert reranker.rerank('q', chunks, 1) == [chunks[1]]
    assert reranker.last_status == 'ok'


def test_budget_failure_does_not_drop_candidates():
    visual = Visual()
    visual.settings = SimpleNamespace(max_images=1, image_token_budget=1000,
        llm=SimpleNamespace(context_window=1100, max_output_tokens=1024))
    chunks = candidates()
    reranker = MultimodalReranker(visual)
    assert reranker.rerank('q', chunks, 3) == chunks and not visual.calls


def test_text_batches_in_mixed_request_use_visual_models_text_path():
    class Mixed(Visual):
        def __init__(self):
            super().__init__()
            self.text_calls = []
        def complete_text(self, prompt, **kwargs):
            self.text_calls.append(prompt)
            items = json.loads(prompt.split('\ndata: ', 1)[1])['candidates']
            return json.dumps({'scores': [{'chunk_id': c['chunk_id'], 'score': 95} for c in items]})
    visual = Mixed()
    chunks = [TextChunk('direct text evidence', 'paper.pdf', 1, 0), *candidates(1)]
    reranker = MultimodalReranker(visual, batch_size=1)
    assert reranker.rerank('q', chunks, 2) == chunks
    assert len(visual.calls) == len(visual.text_calls) == 1
    assert reranker.last_status == 'ok_multimodal'


def test_valid_ties_keep_original_order_and_clear_previous_scores():
    class Equal(Visual):
        def complete(self, prompt, evidence, **kwargs):
            return json.dumps({'scores': [{'chunk_id': c['chunk_id'], 'score': 50} for c in reversed(evidence)]})
    chunks = candidates()
    reranker = MultimodalReranker(Equal())
    assert reranker.rerank('q', chunks, 3) == chunks
    assert len(reranker.last_scores) == 3
    assert reranker.rerank('q', [], 3) == [] and reranker.last_scores == []
