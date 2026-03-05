from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StoryboardVersionRead(BaseModel):
    id: str
    project_id: str
    step_id: str
    version_index: int
    source_attempt: int
    model_provider: str | None
    model_name: str | None
    output_snapshot: dict[str, Any]
    prompt_snapshot: dict[str, Any]
    consistency_score: int | None
    consistency_report: dict[str, Any]
    rollback_reason: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SelectStoryboardVersionPayload(BaseModel):
    created_by: str = "ui-reviewer"
    comment: str | None = None
    scope_type: str = Field(default="step")
