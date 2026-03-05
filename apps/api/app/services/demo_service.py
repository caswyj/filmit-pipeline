from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class DemoCase:
    id: str
    title: str
    description: str
    file_name: str
    recommended_project_name: str
    target_duration_sec: int
    source_path: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _default_1408_path() -> Path:
    if settings.demo_1408_path:
        return Path(settings.demo_1408_path)
    return _repo_root() / "demo_data" / "night_shift_demo" / "source_story.txt"


def _read_text_with_fallbacks(path: Path) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "utf-16le", "gb18030"):
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("utf-8", errors="replace"), "utf-8-replace"


def _demo_1408() -> DemoCase:
    return DemoCase(
        id="1408",
        title="1408",
        description="酒店单场景惊悚短篇，适合演示从文本导入到分步审核的完整链路。",
        file_name="1408.txt",
        recommended_project_name="1408 Demo",
        target_duration_sec=90,
        source_path=_default_1408_path(),
    )


def list_demo_cases() -> list[dict[str, Any]]:
    demos = [_demo_1408()]
    items: list[dict[str, Any]] = []
    for demo in demos:
        available = demo.source_path.exists()
        char_count: int | None = None
        line_count: int | None = None
        if available:
            text, _ = _read_text_with_fallbacks(demo.source_path)
            char_count = len(text)
            line_count = len(text.splitlines())
        items.append(
            {
                "id": demo.id,
                "title": demo.title,
                "description": demo.description,
                "file_name": demo.file_name,
                "recommended_project_name": demo.recommended_project_name,
                "target_duration_sec": demo.target_duration_sec,
                "available": available,
                "char_count": char_count,
                "line_count": line_count,
            }
        )
    return items


def get_demo_case(demo_id: str) -> DemoCase:
    demos = {demo.id: demo for demo in (_demo_1408(),)}
    if demo_id not in demos:
        raise ValueError(f"unknown demo case: {demo_id}")
    return demos[demo_id]
