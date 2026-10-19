from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.adapters.demo_llm import FakeLLMClient
from deepsearch_agent.core.pdf import FakePdfReader


DEMO_SOURCE = "demo://papers/example.pdf"

DEMO_QUESTIONS = (
    "模型怎么训练？",
    "实验用了什么设备？",
)

DEMO_PAGES = [
    "实验使用两张显卡。",
    "模型使用对比学习训练。",
]

DEMO_VECTORS = {
    "实验使用两张显卡。": [0.0, 1.0],
    "模型使用对比学习训练。": [1.0, 0.0],
    "模型怎么训练？": [0.9, 0.1],
    "实验用了什么设备？": [0.1, 0.9],
}


class DemoEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(DEMO_VECTORS[text]) for text in texts]


def build_demo_knowledge_base() -> KnowledgeBase:
    return KnowledgeBase(
        pdf_reader=FakePdfReader(pages=list(DEMO_PAGES)),
        chunk_size=100,
        overlap=0,
        embedding_client=DemoEmbeddingClient(),
        embedding_id="demo-v1",
    )


def build_demo_llm() -> FakeLLMClient:
    return FakeLLMClient(
        response="【固定演示回答】已接入证据 [1]，请查看对应原文。"
    )
