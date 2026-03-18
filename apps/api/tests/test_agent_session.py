from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


SAMPLE_NOVEL_TEXT = """
第1章 雨夜
阿明走进旧楼，看见昏暗走廊里只亮着一盏灯。

第2章 追问
他在楼道尽头追问真相，风声从破碎的窗户里灌进来。
""".strip()


def _new_client(db_file_name: str) -> tuple[TestClient, Path]:
    db_path = Path(f"./{db_file_name}")
    if db_path.exists():
        db_path.unlink()
    app = fresh_app(database_url=f"sqlite:///{db_path}")
    return TestClient(app), db_path


def _cleanup_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()


def _create_project(client: TestClient, name: str) -> str:
    created = client.post("/api/v1/projects", json={"name": name, "target_duration_sec": 120})
    assert created.status_code == 201
    return created.json()["id"]


def _upload_source_document(client: TestClient, project_id: str, text: str = SAMPLE_NOVEL_TEXT) -> None:
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/source-documents",
        files={"file": ("sample.txt", text.encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 201


def _run_step(client: TestClient, project_id: str, step_name: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/steps/{step_name}/run",
        json={"force": True, "params": {}},
    )
    assert response.status_code == 200
    return response.json()


def _propose_action(client: TestClient, project_id: str, *, message: str, page_context: dict | None = None) -> tuple[dict, str]:
    reply = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/default/messages",
        json={"message": message, "page_context": page_context or {}},
    )
    assert reply.status_code == 200
    body = reply.json()
    proposal = next(item for item in body["run"]["tool_calls"] if item["tool_name"] == "propose_write_action")
    return body, proposal["id"]


def _find_step(steps: list[dict], step_name: str) -> dict:
    return next(item for item in steps if item["step_name"] == step_name)


def test_agent_default_session_bootstrap() -> None:
    client, db_path = _new_client("test_n2v_agent_session.db")
    with client:
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

    _cleanup_db(db_path)


def test_agent_read_turn_returns_project_context() -> None:
    client, db_path = _new_client("test_n2v_agent_read_turn.db")
    with client:
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

    _cleanup_db(db_path)


def test_agent_write_intent_requires_explicit_approval() -> None:
    client, db_path = _new_client("test_n2v_agent_write_intent.db")
    with client:
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
        assert body["assistant_message"]["content_json"]["pending_tool_call_id"]
        assert any(item["tool_name"] == "propose_write_action" for item in body["run"]["tool_calls"])

    _cleanup_db(db_path)


def test_agent_actions_endpoint_lists_pending_and_history() -> None:
    client, db_path = _new_client("test_n2v_agent_actions_queue.db")
    with client:
        project_id = _create_project(client, "agent-actions-queue-demo")
        _upload_source_document(client, project_id)

        _, tool_call_id = _propose_action(
            client,
            project_id,
            message="请运行当前步骤",
            page_context={"selected_step_key": "ingest_parse", "selected_step_name": "导入全文"},
        )

        queue = client.get(f"/api/v1/projects/{project_id}/agent/actions")
        assert queue.status_code == 200
        body = queue.json()
        assert len(body["pending"]) == 1
        assert body["pending"][0]["tool_call_id"] == tool_call_id
        assert body["pending"][0]["call_status"] == "REQUIRES_APPROVAL"

        rejected = client.post(f"/api/v1/projects/{project_id}/agent/tool-calls/{tool_call_id}/reject", json={})
        assert rejected.status_code == 200

        queue = client.get(f"/api/v1/projects/{project_id}/agent/actions")
        assert queue.status_code == 200
        body = queue.json()
        assert body["pending"] == []
        assert body["history"][0]["tool_call_id"] == tool_call_id
        assert body["history"][0]["decision_status"] == "REJECTED"

    _cleanup_db(db_path)


def test_agent_can_approve_run_step_execution() -> None:
    client, db_path = _new_client("test_n2v_agent_approve_run_step.db")
    with client:
        project_id = _create_project(client, "agent-run-step-demo")
        _upload_source_document(client, project_id)

        proposed, tool_call_id = _propose_action(
            client,
            project_id,
            message="请运行当前步骤",
            page_context={"selected_step_key": "ingest_parse", "selected_step_name": "导入全文"},
        )
        assert proposed["assistant_message"]["content_json"]["approval_request"]["ready"] is True

        approved = client.post(f"/api/v1/projects/{project_id}/agent/tool-calls/{tool_call_id}/approve", json={})
        assert approved.status_code == 200
        body = approved.json()
        assert body["run"]["status"] == "COMPLETED"
        assert any(item["tool_name"] == "run_step" for item in body["run"]["tool_calls"])
        assert "已执行已批准的 Agent 写操作" in body["assistant_message"]["content_text"]

        steps = client.get(f"/api/v1/projects/{project_id}/steps")
        assert steps.status_code == 200
        ingest_step = _find_step(steps.json(), "ingest_parse")
        assert ingest_step["status"] == "REVIEW_REQUIRED"
        assert ingest_step["attempt"] == 1

    _cleanup_db(db_path)


def test_agent_can_approve_story_bible_rebuild() -> None:
    client, db_path = _new_client("test_n2v_agent_story_bible_rebuild.db")
    with client:
        project_id = _create_project(client, "agent-story-bible-demo")
        _upload_source_document(client, project_id)
        _run_step(client, project_id, "ingest_parse")
        _run_step(client, project_id, "chapter_chunking")

        proposed, tool_call_id = _propose_action(client, project_id, message="请重建 Story Bible")
        assert proposed["assistant_message"]["content_json"]["approval_request"]["display_name"] == "重建 Story Bible"

        approved = client.post(f"/api/v1/projects/{project_id}/agent/tool-calls/{tool_call_id}/approve", json={})
        assert approved.status_code == 200
        body = approved.json()
        assert body["run"]["status"] == "COMPLETED"
        execution_call = next(item for item in body["run"]["tool_calls"] if item["tool_name"] == "rebuild_story_bible")
        assert execution_call["call_status"] == "SUCCEEDED"
        assert "已重建 Story Bible" in body["assistant_message"]["content_text"]

    _cleanup_db(db_path)


def test_agent_feedback_can_generate_prompt_refine_action() -> None:
    client, db_path = _new_client("test_n2v_agent_feedback_refine.db")
    with client:
        project_id = _create_project(client, "agent-feedback-refine-demo")
        _upload_source_document(client, project_id)
        _run_step(client, project_id, "ingest_parse")

        reply, _tool_call_id = _propose_action(
            client,
            project_id,
            message="当前章节剧本太平了，帮我加强冲突和转折后重跑",
            page_context={"selected_step_key": "story_scripting", "selected_step_name": "章节剧本"},
        )
        approval_request = reply["assistant_message"]["content_json"]["approval_request"]
        assert approval_request["display_name"] == "根据批评意见改写提示词并重跑"
        assert approval_request["feedback_summary"]
        assert approval_request["prompt_preview"]

    _cleanup_db(db_path)


def test_agent_can_approve_edit_prompt_regenerate() -> None:
    client, db_path = _new_client("test_n2v_agent_edit_prompt_regen.db")
    with client:
        project_id = _create_project(client, "agent-edit-prompt-demo")
        _upload_source_document(client, project_id)
        _run_step(client, project_id, "ingest_parse")

        proposed, tool_call_id = _propose_action(
            client,
            project_id,
            message="请把当前步骤提示词改成更强调章节转折并重新生成",
            page_context={"selected_step_key": "ingest_parse", "selected_step_name": "导入全文"},
        )
        approval_request = proposed["assistant_message"]["content_json"]["approval_request"]
        assert approval_request["display_name"] == "修改提示词并重生成"
        assert approval_request["ready"] is True

        approved = client.post(f"/api/v1/projects/{project_id}/agent/tool-calls/{tool_call_id}/approve", json={})
        assert approved.status_code == 200
        body = approved.json()
        execution_call = next(item for item in body["run"]["tool_calls"] if item["tool_name"] == "edit_prompt_regenerate")
        assert execution_call["call_status"] == "SUCCEEDED"
        assert "重生成" in body["assistant_message"]["content_text"]

        steps = client.get(f"/api/v1/projects/{project_id}/steps")
        assert steps.status_code == 200
        ingest_step = _find_step(steps.json(), "ingest_parse")
        assert ingest_step["attempt"] == 2
        assert ingest_step["status"] == "REVIEW_REQUIRED"

    _cleanup_db(db_path)


def test_agent_can_reject_pending_action() -> None:
    client, db_path = _new_client("test_n2v_agent_reject_action.db")
    with client:
        project_id = _create_project(client, "agent-reject-demo")
        _upload_source_document(client, project_id)

        _, tool_call_id = _propose_action(
            client,
            project_id,
            message="请运行当前步骤",
            page_context={"selected_step_key": "ingest_parse", "selected_step_name": "导入全文"},
        )

        rejected = client.post(f"/api/v1/projects/{project_id}/agent/tool-calls/{tool_call_id}/reject", json={})
        assert rejected.status_code == 200
        body = rejected.json()
        assert body["run"]["run_mode"] == "approval_reject"
        assert "未发生写入改动" in body["assistant_message"]["content_text"]

        messages = client.get(f"/api/v1/projects/{project_id}/agent/sessions/default/messages")
        assert messages.status_code == 200
        approval_messages = [item for item in messages.json() if item["content_json"].get("approval_request")]
        assert approval_messages[-1]["content_json"]["approval_request"]["decision_status"] == "REJECTED"

        steps = client.get(f"/api/v1/projects/{project_id}/steps")
        assert steps.status_code == 200
        ingest_step = _find_step(steps.json(), "ingest_parse")
        assert ingest_step["attempt"] == 0
        assert ingest_step["status"] == "PENDING"

    _cleanup_db(db_path)
