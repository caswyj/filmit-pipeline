from __future__ import annotations

from pydantic import BaseModel


class PromptTemplateRead(BaseModel):
    step_name: str
    step_display_name: str
    template_id: str
    label: str
    description: str
    system_prompt: str
    task_prompt: str
