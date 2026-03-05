from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_ingest_and_chunking_run_locally_with_chinese_headings() -> None:
    db_path = Path("./test_n2v_local_chunking.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_local_chunking.db")

    chapter_a = "一\n" + ("\n\n".join([f"第一章段落 {i}，气氛逐渐压迫。{'黑夜降临。' * 40}" for i in range(1, 8)]))
    chapter_b = "二\n" + ("\n\n".join([f"第二章段落 {i}，人物开始冲突。{'风声穿过树林。' * 35}" for i in range(1, 7)]))
    source_text = f"宠物公墓\n\n{chapter_a}\n\n{chapter_b}"

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "宠物公墓-demo", "target_duration_sec": 180})
        assert created.status_code == 201
        project = created.json()
        pid = project["id"]

        upload = client.post(
            f"/api/v1/projects/{pid}/source-documents",
            files={"file": ("pet-sematary.txt", source_text.encode("utf-8"), "text/plain")},
        )
        assert upload.status_code == 201

        run = client.post(f"/api/v1/projects/{pid}/run")
        assert run.status_code == 200
        ingest = run.json()["current_step"]
        assert ingest["step_name"] == "ingest_parse"
        assert ingest["model_provider"] == "local"
        assert ingest["model_name"] == "builtin-parser"
        assert ingest["output_ref"]["artifact"]["char_count"] == len(source_text)
        assert ingest["output_ref"]["artifact"]["full_text"] == source_text

        approve = client.post(
            f"/api/v1/projects/{pid}/steps/{ingest['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve.status_code == 200
        assert approve.json()["current_step"]["step_name"] == "chapter_chunking"

        chunk = client.post(
            f"/api/v1/projects/{pid}/steps/chapter_chunking/run",
            json={},
        )
        assert chunk.status_code == 200
        chunk_step = chunk.json()
        assert chunk_step["model_provider"] == "local"
        assert chunk_step["model_name"] == "builtin-chunker"
        artifact = chunk_step["output_ref"]["artifact"]
        assert artifact["chapter_count"] == 2
        assert artifact["segment_count"] >= 2
        assert any(title.startswith("一") for title in artifact["chapter_titles"])
        assert any(title.startswith("二") for title in artifact["chapter_titles"])

        chapters = client.get(f"/api/v1/projects/{pid}/chapters")
        assert chapters.status_code == 200
        chapter_items = chapters.json()
        assert len(chapter_items) >= 2
        assert chapter_items[0]["title"].startswith("一")
        assert any(item["title"].startswith("二") for item in chapter_items)

    if db_path.exists():
        db_path.unlink()
