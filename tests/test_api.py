import pytest
from fastapi.testclient import TestClient

from deepsearch_agent.api import create_app
from deepsearch_agent.embedding_runtime import EmbeddingSettings
from deepsearch_agent.llm_config import LLMSettings

# 显式注入演示配置，避免测试继承开发者本机的 live .env。
DEMO_SETTINGS = dict(
    llm_settings=LLMSettings(backend="demo"),
    embedding_settings=EmbeddingSettings(backend="demo"),
)


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(index_path=tmp_path / "demo.json", **DEMO_SETTINGS)
    ) as client:
        yield client


def test_health_and_initial_documents(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo"}
    assert client.get("/api/documents").json() == []


def test_ingest_then_ask_returns_evidence(client):
    response = client.post("/api/demo/ingest")

    assert response.status_code == 200
    assert response.json()["added_chunks"] == 2

    documents = client.get("/api/documents").json()

    assert len(documents) == 1
    assert documents[0]["chunk_count"] == 2

    response = client.post(
        "/api/ask",
        json={"question": "模型怎么训练？", "top_k": 1},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["mode"] == "demo"
    assert "固定演示回答" in data["text"]
    assert len(data["sources"]) == 1
    assert data["sources"][0]["citation_id"] == 1
    assert data["sources"][0]["page_number"] == 2
    assert data["sources"][0]["text"] == "模型使用对比学习训练。"


def test_repeated_demo_ingest_does_not_duplicate_chunks(client):
    client.post("/api/demo/ingest")
    response = client.post("/api/demo/ingest")

    assert response.json()["added_chunks"] == 0
    assert client.get("/api/documents").json()[0]["chunk_count"] == 2


def test_ask_before_ingest_returns_refusal(client):
    response = client.post(
        "/api/ask",
        json={"question": "模型怎么训练？"},
    )

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert "未检索到可用证据" in response.json()["text"]


@pytest.mark.parametrize(
    "body",
    [
        {"question": "   "},
        {"question": "模型怎么训练？", "top_k": 0},
    ],
)
def test_invalid_request_returns_422(client, body):
    assert client.post("/api/ask", json=body).status_code == 422


def test_unsupported_demo_question_returns_400(client):
    response = client.post(
        "/api/ask",
        json={"question": "没有定义过的演示问题"},
    )

    assert response.status_code == 400
    assert "演示模式仅支持" in response.json()["detail"]
def test_import_is_restored_after_restart(tmp_path):
    index_path = tmp_path / "demo.json"

    with TestClient(create_app(index_path, **DEMO_SETTINGS)) as first:
        response = first.post("/api/demo/ingest")

        assert response.status_code == 200
        assert index_path.exists()

    # 创建新的应用实例，模拟后端重启。
    with TestClient(create_app(index_path, **DEMO_SETTINGS)) as second:
        documents = second.get("/api/documents").json()

        assert len(documents) == 1
        assert documents[0]["chunk_count"] == 2

        response = second.post(
            "/api/ask",
            json={"question": "模型怎么训练？", "top_k": 1},
        )

        assert response.status_code == 200
        assert response.json()["sources"][0]["page_number"] == 2


def test_manual_save_and_load(client):
    client.post("/api/demo/ingest")

    assert client.post("/api/knowledge-base/save").status_code == 200
    assert client.post("/api/knowledge-base/load").status_code == 200

    status = client.get("/api/knowledge-base/status").json()

    assert status["snapshot_exists"] is True
    assert status["dirty"] is False
    assert status["error"] is None


def test_failed_load_preserves_memory_and_blocks_overwrite(tmp_path):
    index_path = tmp_path / "demo.json"

    with TestClient(create_app(index_path, **DEMO_SETTINGS)) as client:
        client.post("/api/demo/ingest")

        # 模拟磁盘快照损坏。
        index_path.write_text("broken json", encoding="utf-8")

        response = client.post("/api/knowledge-base/load")

        assert response.status_code == 409

        # 内存中的文献仍然可用。
        assert client.get("/api/documents").json()[0]["chunk_count"] == 2

        # 不能通过保存按钮悄悄覆盖损坏文件。
        assert client.post("/api/knowledge-base/save").status_code == 409
        assert index_path.read_text(encoding="utf-8") == "broken json"
def test_failed_auto_save_does_not_commit_import(client, monkeypatch):
    from deepsearch_agent.knowledge_base import KnowledgeBase

    def fail_save(self, path):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(KnowledgeBase, "save", fail_save)

    response = client.post("/api/demo/ingest")

    assert response.status_code == 500
    assert client.get("/api/documents").json() == []