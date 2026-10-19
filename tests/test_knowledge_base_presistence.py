import json

import pytest

from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.pdf import FakePdfReader


class StubEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        mapping = {
            "模型使用对比学习训练。": [1.0, 0.0],
            "实验使用两张显卡。": [0.0, 1.0],
            "模型怎么训练？": [0.9, 0.1],
        }
        return [mapping[text] for text in texts]


class QueryOnlyEmbeddingClient:
    """加载后的客户端只处理问题，不能重新计算文档向量。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if texts != ["模型怎么训练？"]:
            raise AssertionError("不应重新生成文档向量")
        return [[0.9, 0.1]]


def make_kb() -> KnowledgeBase:
    return KnowledgeBase(
        pdf_reader=FakePdfReader(
            pages=[
                "实验使用两张显卡。",
                "模型使用对比学习训练。",
            ]
        ),
        chunk_size=100,
        overlap=0,
        embedding_client=StubEmbeddingClient(),
        embedding_id="stub-v1",
    )


def test_saved_index_can_be_loaded_without_reembedding(tmp_path):
    path = tmp_path / "indexes" / "papers.json"

    original = make_kb()
    original.ingest_pdf("papers/demo.pdf")
    original.save(path)

    restored = KnowledgeBase(
        pdf_reader=FakePdfReader(pages=[]),
        embedding_client=QueryOnlyEmbeddingClient(),
        embedding_id="stub-v1",
    )
    restored.load(path)

    result = restored.search("模型怎么训练？", top_k=1)

    assert restored.chunks == original.chunks
    assert restored.chunk_size == 100
    assert restored.overlap == 0
    assert result[0].text == "模型使用对比学习训练。"
    assert result[0].source_path == "papers/demo.pdf"
    assert result[0].page_number == 2


def test_load_rejects_another_embedding_model(tmp_path):
    path = tmp_path / "papers.json"
    original = make_kb()
    original.ingest_pdf("papers/demo.pdf")
    original.save(path)

    restored = KnowledgeBase(
        pdf_reader=FakePdfReader(pages=[]),
        embedding_client=QueryOnlyEmbeddingClient(),
        embedding_id="another-model",
    )

    with pytest.raises(ValueError, match="模型标识"):
        restored.load(path)

    assert restored.chunks == []


def test_invalid_snapshot_does_not_replace_existing_index(tmp_path):
    path = tmp_path / "papers.json"
    kb = make_kb()
    kb.ingest_pdf("papers/demo.pdf")
    kb.save(path)

    # 人为破坏最后一个向量，让它与其他向量维度不同。
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][-1]["vector"] = [1.0, 0.0, 0.0]
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    previous_chunks = list(kb.chunks)

    with pytest.raises(ValueError, match="维度"):
        kb.load(path)

    assert kb.chunks == previous_chunks
    assert kb.search("模型怎么训练？", top_k=1)[0].page_number == 2