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


def test_consistency_failure_rolls_back_to_storyboard_and_preserves_versions(tmp_path) -> None:
    db_path = tmp_path / "test_consistency_rollback.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=101)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "rollback-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        pid = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("story.txt", b"night falls over the city", "text/plain")},
        )
        assert upload.status_code == 201

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        current_step = run.json()["current_step"]

        while current_step["step_name"] != "storyboard_image":
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

        storyboard_step = _run_step(client, pid, "storyboard_image", chapter_id=client.get(f"/api/v1/projects/{pid}/chapters").json()[0]["id"])

        approved_storyboard = client.post(
            f"/api/v1/projects/{pid}/steps/{storyboard_step['id']}/approve",
            json={"scope_type": "step", "created_by": "tester", "chapter_id": client.get(f"/api/v1/projects/{pid}/chapters").json()[0]["id"]},
        )
        assert approved_storyboard.status_code == 200
        next_step = approved_storyboard.json()["current_step"]
        assert next_step["step_name"] == "consistency_check"
        rollback_step = _run_step(client, pid, "consistency_check", chapter_id=client.get(f"/api/v1/projects/{pid}/chapters").json()[0]["id"])
        assert rollback_step["step_name"] == "storyboard_image"
        assert rollback_step["status"] == "REVIEW_REQUIRED"
        assert "rollback_required" in rollback_step["output_ref"]

        versions = client.get(f"/api/v1/projects/{pid}/steps/{rollback_step['id']}/storyboard-versions")
        assert versions.status_code == 200
        version_list = versions.json()
        assert len(version_list) >= 1
        assert version_list[0]["consistency_score"] is not None
        assert version_list[0]["rollback_reason"] is not None
        thumbnail_url = version_list[0]["output_snapshot"]["artifact"]["thumbnail_url"]
        assert thumbnail_url.startswith("/api/v1/local-files/")
        thumbnail = client.get(thumbnail_url)
        assert thumbnail.status_code == 200

        selected = client.post(
            f"/api/v1/projects/{pid}/steps/{rollback_step['id']}/storyboard-versions/{version_list[0]['id']}/select",
            json={"created_by": "tester", "scope_type": "step"},
        )
        assert selected.status_code == 200
        selected_step = selected.json()
        assert selected_step["step_name"] == "storyboard_image"
        assert selected_step["output_ref"]["selected_storyboard_version_id"] == version_list[0]["id"]
