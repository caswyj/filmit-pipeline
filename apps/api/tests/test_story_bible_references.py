from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_ingest_generates_story_bible_reference_images() -> None:
    db_path = Path("./test_n2v_story_bible.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_story_bible.db")
    source_text = (
        "一\n\n路易斯和瑞秋搬到小镇边缘的新家。贾德站在门廊，身后是通往墓地的树林。\n\n"
        "二\n\n艾丽再次提到那片墓地，路易斯穿过树林的小径，墓地石碑排列在雾里。\n\n"
        "三\n\n贾德带着路易斯靠近墓地后的更深处，阴冷树林与石碑反复出现。"
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "story-bible-demo", "target_duration_sec": 120})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={"file": ("novel.txt", source_text.encode("utf-8"), "text/plain")},
        )
        assert upload.status_code == 201

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        artifact = ingest.json()["output_ref"]["artifact"]
        story_bible = artifact["story_bible"]
        assert len(story_bible["characters"]) >= 1
        assert len(story_bible["scenes"]) >= 1
        assert any(item.get("reference_image_url") for item in story_bible["characters"])
        assert any(item.get("reference_image_url") for item in story_bible["scenes"])

        project = client.get(f"/api/v1/projects/{project_id}")
        assert project.status_code == 200
        project_story_bible = project.json()["style_profile"]["story_bible"]
        assert len(project_story_bible["characters"]) >= 1
        assert len(project_story_bible["scenes"]) >= 1

    if db_path.exists():
        db_path.unlink()
