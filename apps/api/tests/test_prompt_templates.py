from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_prompt_templates_and_step_display_names(tmp_path) -> None:
    db_path = tmp_path / "test_prompt_templates.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        templates = client.get("/api/v1/prompt-templates")
        assert templates.status_code == 200
        payload = templates.json()
        assert len(payload) >= 20
        assert any(item["step_name"] == "storyboard_image" for item in payload)

        created = client.post("/api/v1/projects", json={"name": "template-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        pid = created.json()["id"]

        steps = client.get(f"/api/v1/projects/{pid}/steps")
        assert steps.status_code == 200
        first = steps.json()[0]
        assert first["step_display_name"] == "导入全文"

        scripting_templates = client.get("/api/v1/prompt-templates?step_name=story_scripting")
        assert scripting_templates.status_code == 200
        labels = {item["label"] for item in scripting_templates.json()}
        assert {"标准剧本", "电影叙事", "人物驱动"} <= labels
