from deepsearch_agent.qa import Answer, SourceReference
from deepsearch_agent.storage import JsonlAnswerStore


def test_store_can_append_and_load_answer(tmp_path):
    path = tmp_path / "answers.jsonl"
    store = JsonlAnswerStore(path)

    answer = Answer(
        text="RAG 可以提升回答的可追溯性。",
        sources=[
            SourceReference(
                source_path="papers/rag.pdf",
                page_number=2,
                text="RAG 使用外部检索内容增强生成。",
            )
        ],
    )

    store.append(
        question="RAG 有什么作用？",
        answer=answer,
    )

    records = store.list_records()

    assert len(records) == 1
    assert records[0]["question"] == "RAG 有什么作用？"
    assert records[0]["answer"]["text"] == "RAG 可以提升回答的可追溯性。"
    assert records[0]["answer"]["sources"][0]["page_number"] == 2