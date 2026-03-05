from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_pipeline_end_to_end() -> None:
    db_path = Path("./test_n2v.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v.db")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "demo", "target_duration_sec": 90})
        assert created.status_code == 201
        project = created.json()
        pid = project["id"]

        providers = client.get("/api/v1/providers/models")
        assert providers.status_code == 200
        assert len(providers.json()) > 0

        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("novel.txt", b"chapter1\nhero enters city", "text/plain")},
        )
        assert upload.status_code == 201

        docs = client.get(f"/api/v1/projects/{pid}/source-documents")
        assert docs.status_code == 200
        assert len(docs.json()) == 1
        assert docs.json()[0]["file_type"] == "txt"

        bind = client.post(
            f"/api/v1/projects/{pid}/model-bindings",
            json={
                "bindings": {
                    "ingest_parse": [{"provider": "deepseek", "model": "deepseek-chat"}],
                }
            },
        )
        assert bind.status_code == 200

        steps = client.get(f"/api/v1/projects/{pid}/steps")
        assert steps.status_code == 200
        assert len(steps.json()) == 8

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        first = run.json()["current_step"]
        assert first["step_name"] == "ingest_parse"
        assert first["status"] in {"REVIEW_REQUIRED", "REWORK_REQUESTED"}

        regen = client.post(
            f"/api/v1/projects/{pid}/steps/{first['id']}/edit-prompt-regenerate",
            json={
                "scope_type": "step",
                "created_by": "tester",
                "task_prompt": "请强化章节切分信息的结构化输出",
                "params": {"temperature": 0.2},
            },
        )
        assert regen.status_code == 200
        regen_step = regen.json()
        assert regen_step["status"] in {"REVIEW_REQUIRED", "REWORK_REQUESTED"}

        switch_model = client.post(
            f"/api/v1/projects/{pid}/steps/{first['id']}/switch-model-rerun",
            json={
                "scope_type": "step",
                "created_by": "tester",
                "provider": "google",
                "model_name": "gemini-2.5-flash-lite",
                "params": {},
            },
        )
        assert switch_model.status_code == 200
        switched_step = switch_model.json()
        assert switched_step["model_provider"] == "local"
        assert switched_step["model_name"] == "builtin-parser"

        approve = client.post(
            f"/api/v1/projects/{pid}/steps/{first['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve.status_code == 200
        approve_data = approve.json()
        assert approve_data["status"] in {"RUNNING", "REVIEW_REQUIRED"}
        assert approve_data["current_step"] is not None
        assert approve_data["current_step"]["step_name"] == "chapter_chunking"

        timeline = client.get(f"/api/v1/projects/{pid}/timeline")
        assert timeline.status_code == 200
        assert len(timeline.json()["step_summaries"]) == 8

    if db_path.exists():
        db_path.unlink()
