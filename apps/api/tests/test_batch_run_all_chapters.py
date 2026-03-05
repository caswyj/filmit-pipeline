from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_run_current_step_for_all_chapters() -> None:
    db_path = Path("./test_n2v_batch.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch.db")
    source_text = (
        "一\n\n路易斯走进小镇，贾德在门口迎接他。墓地后方的树林始终阴沉。\n\n"
        "二\n\n瑞秋带着艾丽回家，公路另一侧的墓地让所有人都感到不安。"
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "batch-demo", "target_duration_sec": 120})
        project_id = created.json()["id"]

        client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={"file": ("novel.txt", source_text.encode("utf-8"), "text/plain")},
        )

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200

        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200
        approve_chunk = client.post(
            f"/api/v1/projects/{project_id}/steps/{chunk.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_chunk.status_code == 200

        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/story_scripting/run-all-chapters",
            json={"force": True},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert payload["step_name"] == "story_scripting"
        assert payload["succeeded"] >= 2
        assert payload["failed"] == 0

    if db_path.exists():
        db_path.unlink()
