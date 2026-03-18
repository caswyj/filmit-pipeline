from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StepRunPayload(BaseModel):
    force: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    chapter_id: str | None = None


class StepPromptPreviewPayload(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    chapter_id: str | None = None
    system_prompt: str | None = None
    task_prompt: str | None = None


class BatchStepRunPayload(BaseModel):
    force: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class StepRead(BaseModel):
    id: str
    project_id: str
    step_name: str
    step_display_name: str
    step_order: int
    status: str
    input_ref: dict[str, Any]
    output_ref: dict[str, Any]
    model_provider: str | None
    model_name: str | None
    attempt: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectRunResponse(BaseModel):
    project_id: str
    status: str
    current_step: StepRead | None = None


class StepPromptPreviewShotRead(BaseModel):
    shot_index: int
    title: str
    motion_directive: str | None = None
    reference_image_url: str | None = None
    prompt: str


class StepPromptPreviewRead(BaseModel):
    project_id: str
    step_name: str
    chapter_id: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_profile: str | None = None
    prompt_profile_label: str | None = None
    chapter_prompt: str | None = None
    shot_prompts: list[StepPromptPreviewShotRead] = Field(default_factory=list)
    prompt_summary: dict[str, Any] = Field(default_factory=dict)


class BatchStepChapterResultRead(BaseModel):
    chapter_id: str
    chapter_title: str
    status: str
    detail: str
    estimated_cost: float | None = None


class BatchStepRunResponse(BaseModel):
    project_id: str
    step_name: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    total_estimated_cost: float = 0.0
    chapter_results: list[BatchStepChapterResultRead]
    current_step: StepRead | None = None


class AssetRead(BaseModel):
    id: str
    step_id: str | None
    asset_type: str
    storage_key: str
    mime_type: str
    meta: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ExportRead(BaseModel):
    id: str
    project_id: str
    status: str
    output_key: str | None
    error_message: str | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}
