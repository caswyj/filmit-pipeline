from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from tests.helpers import fresh_app


def test_ingest_generates_story_bible_reference_images() -> None:
    db_path = Path("./test_n2v_story_bible.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_story_bible.db")
    source_text = (
        "一\n\n路易斯和瑞琪儿搬到小镇边缘的新家。贾德站在门廊迎接路易斯，身后是通往墓地的树林。"
        "艾丽抱着猫从厨房跑向门口，路易斯看见墓地石碑在雾里。\n\n"
        "二\n\n艾丽再次提到那片墓地，路易斯和贾德一起穿过树林的小径。"
        "瑞琪儿在厨房等他们回来，墓地石碑与树林反复出现。\n\n"
        "三\n\n贾德带着路易斯靠近墓地后的更深处，艾丽也在门廊远远看着。"
        "瑞琪儿回到厨房，树林、墓地和门廊在同一夜里反复出现。"
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
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200

        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200
        artifact = chunk.json()["output_ref"]["artifact"]
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


def test_reference_image_data_url_downsamples_large_local_images(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_resize.db'}")

    from app.services.pipeline_service import PipelineService

    source = Image.effect_noise((1800, 1200), 120).convert("RGB")
    path = tmp_path / "large-reference.png"
    source.save(path, format="PNG")

    service = PipelineService.__new__(PipelineService)
    data_url = service._reference_image_data_url(str(path), None)

    assert isinstance(data_url, str)
    assert data_url.startswith("data:image/jpeg;base64,")

    encoded = data_url.split(",", 1)[1]
    decoded = base64.b64decode(encoded)
    with Image.open(BytesIO(decoded)) as image:
        assert max(image.size) <= 640
    assert len(decoded) < path.stat().st_size
