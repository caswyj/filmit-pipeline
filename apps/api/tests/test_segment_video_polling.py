from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import fresh_app

CHAPTER_SCOPED_STEPS = {"story_scripting", "shot_detailing", "storyboard_image", "consistency_check", "segment_video"}


def _run_step(client: TestClient, project_id: str, step_name: str, chapter_id: str | None = None) -> dict:
    payload = {"force": True, "params": {}}
    if chapter_id:
        payload["chapter_id"] = chapter_id
        payload["params"]["chapter_id"] = chapter_id
    run = client.post(f"/api/v1/projects/{project_id}/steps/{step_name}/run", json=payload)
    assert run.status_code == 200
    return run.json()


def test_segment_video_polling_generates_preview_asset(tmp_path) -> None:
    db_path = tmp_path / "test_segment_video_polling.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=1)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "video-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        pid = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("story.txt", b"night chase through neon city", "text/plain")},
        )
        assert upload.status_code == 201

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        current_step = run.json()["current_step"]

        while current_step["step_name"] != "segment_video":
            approved = client.post(
                f"/api/v1/projects/{pid}/steps/{current_step['id']}/approve",
                json={"scope_type": "step", "created_by": "tester"},
            )
            assert approved.status_code == 200
            current_step = approved.json()["current_step"]
            assert current_step is not None
            if current_step["status"] == "PENDING":
                chapter_id = None
                if current_step["step_name"] in CHAPTER_SCOPED_STEPS:
                    chapters = client.get(f"/api/v1/projects/{pid}/chapters")
                    assert chapters.status_code == 200
                    chapter_id = chapters.json()[0]["id"]
                current_step = _run_step(client, pid, current_step["step_name"], chapter_id=chapter_id)

        current_step = _run_step(client, pid, "segment_video", chapter_id=client.get(f"/api/v1/projects/{pid}/chapters").json()[0]["id"])
        assert current_step["status"] == "REVIEW_REQUIRED"
        artifact = current_step["output_ref"]["artifact"]
        polling = current_step["output_ref"].get("polling") or {}
        if polling:
            assert polling["final_status"] == "completed"
        preview_url = current_step["output_ref"]["artifact"]["preview_url"]
        export_url = current_step["output_ref"]["artifact"]["export_url"]
        assert preview_url.startswith("/api/v1/local-files/")
        assert export_url.startswith("/api/v1/local-files/")
        assert artifact["artifact_mode"] in {"motion_preview_segment", "real_generated_shot_clips", "hybrid_generated_shot_clips"}
        assert artifact["motion_validation"]["sample_count"] >= 2
        preview = client.get(preview_url)
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("video/mp4")
        export = client.get(f"{export_url}?download=1")
        assert export.status_code == 200
