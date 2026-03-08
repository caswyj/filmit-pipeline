from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def _run_step(client: TestClient, project_id: str, step_name: str, chapter_id: str | None = None) -> dict:
    payload = {"force": True, "params": {}}
    if chapter_id:
        payload["chapter_id"] = chapter_id
        payload["params"]["chapter_id"] = chapter_id
    res = client.post(f"/api/v1/projects/{project_id}/steps/{step_name}/run", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def _approve_step(client: TestClient, project_id: str, step_id: str, chapter_id: str | None = None) -> dict:
    payload = {"scope_type": "step", "created_by": "tester"}
    if chapter_id:
        payload["chapter_id"] = chapter_id
    res = client.post(f"/api/v1/projects/{project_id}/steps/{step_id}/approve", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def test_storyboard_prompt_softens_explicit_bare_clothing_language(tmp_path) -> None:
    db_path = tmp_path / "test_storyboard_prompt_safety.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=1)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "storyboard-safety-demo", "target_duration_sec": 30})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "story.txt",
                    (
                        "Chapter 1\n\n"
                        "路易斯回家时，妻子在门口迎着他，穿着乳罩和半透明的短裤，别的什么都没穿。"
                        "两人轻轻拥抱，并谈到孩子们暂时由邻居照看。"
                    ).encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201

        current_step = client.post(f"/api/v1/projects/{project_id}/run").json()["current_step"]
        current_step = _approve_step(client, project_id, current_step["id"])["current_step"]
        current_step = _run_step(client, project_id, current_step["step_name"])
        _approve_step(client, project_id, current_step["id"])

        chapter_id = client.get(f"/api/v1/projects/{project_id}/chapters").json()[0]["id"]
        for step_name in ["story_scripting", "shot_detailing"]:
            current_step = _run_step(client, project_id, step_name, chapter_id)
            _approve_step(client, project_id, current_step["id"], chapter_id)

        storyboard = _run_step(client, project_id, "storyboard_image", chapter_id)
        first_prompt = storyboard["output_ref"]["artifact"]["frames"][0]["prompt"]

        assert "乳罩" not in first_prompt
        assert "半透明" not in first_prompt
        assert "什么都没穿" not in first_prompt
        assert "PG-13 framing" in first_prompt
        assert "fully clothed adult characters" in first_prompt

