from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import fresh_app


def test_demo_case_can_be_imported_from_server_side_text(tmp_path: Path) -> None:
    db_path = tmp_path / "test_demo_import.db"
    demo_path = tmp_path / "demo_1408.txt"
    demo_text = "1408 房门在身后关上。\n走廊里的声音一下子消失了。"
    demo_path.write_text(demo_text, encoding="utf-8")

    app = fresh_app(
        database_url=f"sqlite:///{db_path}",
        demo_1408_path=str(demo_path),
    )

    with TestClient(app) as client:
        demos = client.get("/api/v1/demo-cases")
        assert demos.status_code == 200
        demo_case = demos.json()[0]
        assert demo_case["id"] == "1408"
        assert demo_case["available"] is True
        assert demo_case["char_count"] == len(demo_text)

        imported = client.post(
            "/api/v1/demo-cases/1408/import",
            json={"name": "1408 Web Demo", "target_duration_sec": 75},
        )
        assert imported.status_code == 201
        project = imported.json()
        assert project["name"] == "1408 Web Demo"
        assert project["style_profile"]["demo_case"] == "1408"

        docs = client.get(f"/api/v1/projects/{project['id']}/source-documents")
        assert docs.status_code == 200
        source_doc = docs.json()[0]
        assert source_doc["file_name"] == "1408.txt"
        assert source_doc["parse_status"] == "IMPORTED_DEMO"

        run = client.post(f"/api/v1/projects/{project['id']}/run")
        assert run.status_code == 200
        current = run.json()["current_step"]
        assert current["step_name"] == "ingest_parse"
        assert current["input_ref"]["source_document"]["content"] == demo_text
