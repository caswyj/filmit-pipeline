from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_uploaded_txt_is_included_in_step_input() -> None:
    db_path = Path("./test_n2v_source.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_source.db")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "1408-demo", "target_duration_sec": 90})
        assert created.status_code == 201
        project = created.json()
        pid = project["id"]

        source_text = "房门关上后，1408 的空气像在呼吸。\n迈克站在门边，没有立刻开灯。"
        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("1408.txt", source_text.encode("utf-8"), "text/plain")},
        )
        assert upload.status_code == 201

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        current = run.json()["current_step"]
        assert current["step_name"] == "ingest_parse"

        source_document = current["input_ref"]["source_document"]
        assert source_document["file_name"] == "1408.txt"
        assert source_document["file_type"] == "txt"
        assert source_document["encoding"] == "utf-8-sig"
        assert source_document["char_count"] == len(source_text)
        assert "1408 的空气像在呼吸" in source_document["content_excerpt"]
        assert source_document["content"] == source_text

    if db_path.exists():
        db_path.unlink()
