from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_style_presets_and_story_bible_are_injected(tmp_path) -> None:
    db_path = tmp_path / "test_style_profile.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        presets = client.get("/api/v1/style-presets")
        assert presets.status_code == 200
        preset_ids = {item["id"] for item in presets.json()}
        assert {"cinematic", "cyberpunk", "gothic", "gloom_noir", "chibi", "realistic"} <= preset_ids

        created = client.post(
            "/api/v1/projects",
            json={
                "name": "style-demo",
                "target_duration_sec": 90,
                "style_profile": {
                    "preset_id": "cyberpunk",
                    "custom_style": "废土宗教机械感",
                    "custom_directives": "强调潮湿金属、橙青对撞、低机位压迫感",
                },
            },
        )
        assert created.status_code == 201
        pid = created.json()["id"]
        assert created.json()["style_profile"]["preset_id"] == "cyberpunk"

        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("story.txt", b"hero enters neon city", "text/plain")},
        )
        assert upload.status_code == 201

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        current_step = run.json()["current_step"]
        assert current_step["input_ref"]["style_profile"]["preset_id"] == "cyberpunk"
        assert current_step["input_ref"]["story_bible"]["visual_style"]["custom_style"] == "废土宗教机械感"
        assert "风格圣经约束" in current_step["output_ref"]["prompt"]["style"]
