#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"
LIB_ROOTS = [
    ROOT / "libs" / "provider_adapters",
    ROOT / "libs" / "workflow_engine",
    ROOT / "libs" / "consistency_engine",
]

for item in [API_ROOT, *LIB_ROOTS]:
    sys.path.insert(0, str(item))


def main() -> int:
    source_path = ROOT / "demo_data" / "night_shift_demo" / "source_story.txt"
    if not source_path.exists():
        print(f"missing demo input: {source_path}", file=sys.stderr)
        return 1

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "demo_runs" / "1408" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    os.environ["N2V_DATABASE_URL"] = f"sqlite:///{run_dir / 'demo.db'}"

    from app.main import app

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "name": "1408-demo",
                "description": "Local demo run using Stephen King 1408",
                "target_duration_sec": 90,
            },
        )
        created.raise_for_status()
        project = created.json()
        project_id = project["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={"file": ("1408.txt", source_path.read_bytes(), "text/plain")},
        )
        upload.raise_for_status()
        uploaded = upload.json()

        run = client.post(f"/api/v1/projects/{project_id}/run")
        run.raise_for_status()
        run_data = run.json()
        current = run_data["current_step"]
        source_document = current["input_ref"]["source_document"]

        summary = {
            "project": {
                "id": project_id,
                "name": project["name"],
                "status": run_data["status"],
            },
            "upload": {
                "document_id": uploaded["id"],
                "file_name": uploaded["file_name"],
                "storage_key": uploaded["storage_key"],
                "parse_status": uploaded["parse_status"],
            },
            "step": {
                "id": current["id"],
                "step_name": current["step_name"],
                "status": current["status"],
                "model_provider": current["model_provider"],
                "model_name": current["model_name"],
                "artifact_id": current["output_ref"]["artifact"]["artifact_id"],
            },
            "source_document": {
                "file_type": source_document["file_type"],
                "encoding": source_document.get("encoding"),
                "char_count": source_document.get("char_count"),
                "line_count": source_document.get("line_count"),
                "content_excerpt": source_document.get("content_excerpt", "")[:500],
                "content_truncated": source_document.get("content_truncated"),
            },
        }

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"demo run complete: {summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
