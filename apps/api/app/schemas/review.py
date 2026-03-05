from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ScopeType = Literal["shot", "chapter", "step"]


class ReviewBasePayload(BaseModel):
    scope_type: ScopeType = "step"
    created_by: str = "human-reviewer"
    comment: str | None = None
    chapter_id: str | None = None


class ApprovePayload(ReviewBasePayload):
    pass


class EditContinuePayload(ReviewBasePayload):
    editor_payload: dict[str, Any] = Field(default_factory=dict)


class EditPromptRegeneratePayload(ReviewBasePayload):
    task_prompt: str = Field(min_length=1)
    system_prompt: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class SwitchModelRerunPayload(ReviewBasePayload):
    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
