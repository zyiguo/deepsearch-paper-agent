from fastapi.testclient import TestClient

from deepsearch_agent.api import create_app
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings
from deepsearch_agent.adapters.llm_config import LLMSettings

# 显式注入演示配置，避免测试继承开发者本机的 live .env。
DEMO_SETTINGS = dict(
    llm_settings=LLMSettings(backend="demo"),
    embedding_settings=EmbeddingSettings(backend="demo"),
)


class RecordingClient:
    def __init__(self):
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "固定测试回答。[1]"


def test_sessions_preserve_history_sources_and_isolation(tmp_path):
    index_path = tmp_path / "demo.json"
    app = create_app(index_path, **DEMO_SETTINGS)
    recorder = RecordingClient()

    with TestClient(app) as client:
        app.state.answer_service.client = recorder

        assert client.post("/api/demo/ingest").status_code == 200

        first_response = client.post(
            "/api/sessions",
            json={"title": "训练方法研究"},
        )
        assert first_response.status_code == 201
        first_id = first_response.json()["id"]

        second_id = client.post(
            "/api/sessions",
            json={"title": "独立研究"},
        ).json()["id"]

        first_turn = client.post(
            f"/api/sessions/{first_id}/turns",
            json={"question": "模型怎么训练？", "top_k": 1},
        )

        assert first_turn.status_code == 201
        assert first_turn.json()["answer"]["sources"][0]["page_number"] == 2

        second_turn = client.post(
            f"/api/sessions/{first_id}/turns",
            json={"question": "实验用了什么设备？", "top_k": 1},
        )

        assert second_turn.status_code == 201
        assert second_turn.json()["answer"]["sources"][0]["page_number"] == 1

        # 第二轮 Prompt 确实包含上一轮问题和回答。
        assert "历史对话" in recorder.prompts[1]
        assert "模型怎么训练？" in recorder.prompts[1]
        assert "固定测试回答。[1]" in recorder.prompts[1]

        client.post(
            f"/api/sessions/{second_id}/turns",
            json={"question": "实验用了什么设备？", "top_k": 1},
        )

        # 新会话不能携带另一个会话的历史。
        assert "历史对话" not in recorder.prompts[2]

        detail = client.get(f"/api/sessions/{first_id}").json()
        assert len(detail["turns"]) == 2

    # 新应用读取同一个数据库，验证重启恢复。
    with TestClient(create_app(index_path, **DEMO_SETTINGS)) as restarted:
        assert len(restarted.get("/api/sessions").json()) == 2

        detail = restarted.get(f"/api/sessions/{first_id}").json()

        assert len(detail["turns"]) == 2
        assert detail["turns"][0]["answer"]["sources"][0]["page_number"] == 2
        assert detail["turns"][1]["answer"]["sources"][0]["page_number"] == 1


def test_unknown_session_returns_404(tmp_path):
    with TestClient(create_app(tmp_path / "demo.json", **DEMO_SETTINGS)) as client:
        assert client.get("/api/sessions/missing").status_code == 404

        response = client.post(
            "/api/sessions/missing/turns",
            json={"question": "模型怎么训练？"},
        )

        assert response.status_code == 404