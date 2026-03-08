from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def _run_step(client: TestClient, project_id: str, step_name: str, chapter_id: str | None = None) -> dict:
    payload = {"force": True, "params": {}}
    if chapter_id:
        payload["chapter_id"] = chapter_id
        payload["params"]["chapter_id"] = chapter_id
    run = client.post(f"/api/v1/projects/{project_id}/steps/{step_name}/run", json=payload)
    assert run.status_code == 200, run.text
    return run.json()


def _approve_step(client: TestClient, project_id: str, step_id: str, chapter_id: str | None = None) -> dict:
    payload = {"scope_type": "step", "created_by": "tester"}
    if chapter_id:
        payload["chapter_id"] = chapter_id
    res = client.post(f"/api/v1/projects/{project_id}/steps/{step_id}/approve", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def test_render_final_creates_real_mp4(tmp_path) -> None:
    db_path = tmp_path / "test_final_export.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=1)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "export-demo", "target_duration_sec": 24})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "story.txt",
                    b"Chapter 1\n\nLouis arrives at the old house.\n\nA shadow moves past the trees.",
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201

        current_step = client.post(f"/api/v1/projects/{project_id}/run").json()["current_step"]
        current_step = _approve_step(client, project_id, current_step["id"])["current_step"]
        current_step = _run_step(client, project_id, current_step["step_name"])
        current_step = _approve_step(client, project_id, current_step["id"])["current_step"]

        chapter_id = client.get(f"/api/v1/projects/{project_id}/chapters").json()[0]["id"]
        for step_name in [
            "story_scripting",
            "shot_detailing",
            "storyboard_image",
            "consistency_check",
            "segment_video",
            "stitch_subtitle_tts",
        ]:
            current_step = _run_step(client, project_id, step_name, chapter_id if step_name != "stitch_subtitle_tts" else None)
            approved = _approve_step(client, project_id, current_step["id"], chapter_id if step_name != "stitch_subtitle_tts" else None)
            current_step = approved["current_step"]

        render = client.post(f"/api/v1/projects/{project_id}/render/final")
        assert render.status_code == 200, render.text
        output_key = render.json()["output_key"]
        assert isinstance(output_key, str)
        output_path = Path(output_key)
        assert output_path.exists()
        assert output_path.suffix == ".mp4"
        assert output_path.stat().st_size > 0


def test_generate_final_cut_runs_stitch_step_and_exports(tmp_path) -> None:
    db_path = tmp_path / "test_generate_final_cut.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=1)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "final-cut-demo", "target_duration_sec": 24})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "story.txt",
                    b"Chapter 1\n\nLouis arrives at the old house.\n\nA shadow moves past the trees.",
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
        for step_name in [
            "story_scripting",
            "shot_detailing",
            "storyboard_image",
            "consistency_check",
            "segment_video",
        ]:
            current_step = _run_step(client, project_id, step_name, chapter_id)
            _approve_step(client, project_id, current_step["id"], chapter_id)

        render = client.post(f"/api/v1/projects/{project_id}/final-cut")
        assert render.status_code == 200, render.text
        output_key = render.json()["output_key"]
        assert isinstance(output_key, str)
        output_path = Path(output_key)
        assert output_path.exists()
        assert output_path.suffix == ".mp4"
        assert output_path.stat().st_size > 0

        steps = client.get(f"/api/v1/projects/{project_id}/steps").json()
        stitch_step = next(item for item in steps if item["step_name"] == "stitch_subtitle_tts")
        artifact = stitch_step["output_ref"]["artifact"]
        assert artifact["segment_count"] >= 1
        assert artifact["narration_text"]
        assert artifact["subtitle_entries"]
        assert "章节剧本已生成" not in artifact["narration_text"]
