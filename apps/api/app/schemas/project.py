from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    target_duration_sec: int = Field(default=120, ge=15, le=7200)
    input_path: str | None = None
    output_path: str | None = None
    style_profile: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    target_duration_sec: int | None = Field(default=None, ge=15, le=7200)
    input_path: str | None = None
    output_path: str | None = None
    style_profile: dict[str, Any] | None = None


class ModelBindingPayload(BaseModel):
    bindings: dict[str, list[dict[str, str]]]


class ProjectRead(BaseModel):
    id: str
    name: str
    description: str | None
    status: str
    target_duration_sec: int
    style_profile: dict[str, Any]
    model_bindings: dict[str, Any]
    input_path: str | None
    output_path: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectTimelineRead(BaseModel):
    project_id: str
    target_duration_sec: int
    step_summaries: list[dict[str, Any]]
