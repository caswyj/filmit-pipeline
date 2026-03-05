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


def test_storyboard_step_generates_full_gallery_and_export_bundle(tmp_path) -> None:
    db_path = tmp_path / "test_storyboard_gallery.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}", consistency_threshold=1)

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "storyboard-gallery-demo", "target_duration_sec": 48})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "story.txt",
                    (
                        "Chapter 1\n\nLouis walks through the old yard.\n\n"
                        "He hears a voice in the dark.\n\n"
                        "The road flashes with truck lights.\n\n"
                        "Church stares from the threshold."
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
        artifact = storyboard["output_ref"]["artifact"]
        gallery = storyboard["output_ref"]["storyboard_gallery"]

        assert artifact["frame_count"] >= 1
        assert len(artifact["frames"]) == artifact["frame_count"]
        assert gallery["frame_count"] == artifact["frame_count"]
        assert gallery["gallery_export_url"].startswith("/api/v1/local-files/")

        contact_sheet = client.get(artifact["thumbnail_url"])
        assert contact_sheet.status_code == 200
        assert contact_sheet.headers["content-type"].startswith("image/png")

        first_frame = artifact["frames"][0]
        frame_res = client.get(first_frame["image_url"])
        assert frame_res.status_code == 200
        assert frame_res.headers["content-type"].startswith("image/png")

        bundle_res = client.get(f"{gallery['gallery_export_url']}?download=1")
        assert bundle_res.status_code == 200
        assert "zip" in bundle_res.headers["content-type"] or bundle_res.headers["content-type"].startswith("application/octet-stream")
