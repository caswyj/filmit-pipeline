from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings


def storage_root() -> Path:
    root = Path(settings.generated_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def sanitize_component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned or "project"


def project_root(project_id: str, project_name: str | None = None) -> Path:
    suffix = sanitize_component(project_name or "project")
    target = storage_root() / f"{suffix}-{project_id}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def project_category_dir(project_id: str, project_name: str | None, category: str) -> Path:
    target = project_root(project_id, project_name) / sanitize_component(category)
    target.mkdir(parents=True, exist_ok=True)
    return target


def step_category(step_name: str) -> str:
    mapping = {
        "ingest_parse": "texts",
        "chapter_chunking": "chapters",
        "story_scripting": "scripts",
        "shot_detailing": "shots",
        "storyboard_image": "storyboards",
        "consistency_check": "consistency",
        "segment_video": "videos",
        "stitch_subtitle_tts": "audio",
    }
    return mapping.get(step_name, "artifacts")
