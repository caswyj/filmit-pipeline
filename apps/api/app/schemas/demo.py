from __future__ import annotations

from pydantic import BaseModel, Field


class DemoCaseRead(BaseModel):
    id: str
    title: str
    description: str
    file_name: str
    recommended_project_name: str
    target_duration_sec: int
    available: bool
    char_count: int | None = None
    line_count: int | None = None


class DemoImportPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    target_duration_sec: int | None = Field(default=None, ge=15, le=7200)
