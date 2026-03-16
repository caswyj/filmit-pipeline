from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_agent_default_session_bootstrap() -> None:
    db_path = Path("./test_n2v_agent_session.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_agent_session.db")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "agent-demo", "target_duration_sec": 120})
        assert created.status_code == 201
        project_id = created.json()["id"]

        session = client.get(f"/api/v1/projects/{project_id}/agent/sessions/default")
        assert session.status_code == 200
        body = session.json()
        assert body["project_id"] == project_id
        assert body["is_default"] is True
        assert body["approval_mode"] == "explicit_write_confirmation"
        assert body["retrieval_mode"] == "local_lightweight_index"

        messages = client.get(f"/api/v1/projects/{project_id}/agent/sessions/default/messages")
        assert messages.status_code == 200
        items = messages.json()
        assert len(items) == 1
        assert items[0]["role"] == "assistant"
        assert "单项目单对话" in items[0]["content_text"]

    if db_path.exists():
        db_path.unlink()


def test_agent_read_turn_returns_project_context() -> None:
    db_path = Path("./test_n2v_agent_read_turn.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_agent_read_turn.db")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "agent-read-demo", "target_duration_sec": 90})
        assert created.status_code == 201
        project_id = created.json()["id"]

        reply = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions/default/messages",
            json={
                "message": "当前项目卡在哪一步？",
                "page_context": {"selected_step_name": "章节剧本"},
            },
        )
        assert reply.status_code == 200
        body = reply.json()
        assert body["run"]["status"] == "COMPLETED"
        assert body["assistant_message"]["role"] == "assistant"
        assert "项目概览" in body["assistant_message"]["content_text"]
        assert body["run"]["tool_calls"][0]["tool_name"] == "get_project_overview"

    if db_path.exists():
        db_path.unlink()


def test_agent_write_intent_requires_explicit_approval() -> None:
    db_path = Path("./test_n2v_agent_write_intent.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_agent_write_intent.db")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "agent-write-demo", "target_duration_sec": 90})
        assert created.status_code == 201
        project_id = created.json()["id"]

        reply = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions/default/messages",
            json={
                "message": "请帮我重跑当前步骤并修改提示词",
                "page_context": {"selected_step_name": "分镜校核"},
            },
        )
        assert reply.status_code == 200
        body = reply.json()
        assert body["run"]["status"] == "WAITING_APPROVAL"
        approval_request = body["assistant_message"]["content_json"]["approval_request"]
        assert approval_request["status"] == "REQUIRES_USER_CONFIRMATION"
        assert "写操作" in approval_request["reason"]
        assert any(item["tool_name"] == "propose_write_action" for item in body["run"]["tool_calls"])

    if db_path.exists():
        db_path.unlink()
