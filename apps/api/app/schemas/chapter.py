from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ChapterRead(BaseModel):
    id: str
    chapter_index: int
    chunk_index: int
    title: str
    summary: str
    content_excerpt: str
    stage_status: str
    stage_map: dict[str, str]
    consistency_score: int | None = None
    meta: dict[str, Any]
