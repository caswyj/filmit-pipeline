from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentSessionRead(BaseModel):
    id: str
    project_id: str
    title: str
    status: str
    session_kind: str
    is_default: bool
    agent_provider: str
    agent_model_name: str
    approval_mode: str
    retrieval_mode: str
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentMessageRead(BaseModel):
    id: str
    session_id: str
    project_id: str
    run_id: str | None
    role: str
    content_text: str
    content_json: dict[str, Any]
    visibility: str
    token_estimate: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentToolCallRead(BaseModel):
    id: str
    run_id: str
    session_id: str
    project_id: str
    tool_name: str
    call_status: str
    args_json: dict[str, Any]
    result_summary: str | None
    result_json: dict[str, Any]
    approval_policy: str
    requires_user_confirmation: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class AgentRunRead(BaseModel):
    id: str
    session_id: str
    project_id: str
    status: str
    run_mode: str
    input_message_id: str | None
    output_message_id: str | None
    agent_provider: str
    agent_model_name: str
    error_message: str | None
    meta: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    tool_calls: list[AgentToolCallRead] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class AgentSendMessagePayload(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    page_context: dict[str, Any] = Field(default_factory=dict)


class AgentToolDecisionPayload(BaseModel):
    comment: str | None = None


class AgentActionItemRead(BaseModel):
    tool_call_id: str
    call_status: str
    requested_action: str
    display_name: str | None = None
    scope_summary: str | None = None
    ready: bool
    missing_fields: list[str] = Field(default_factory=list)
    user_visible_summary: str | None = None
    estimated_cost: float | None = None
    estimated_cost_summary: str | None = None
    cost_source: str | None = None
    prompt_preview: str | None = None
    feedback_summary: str | None = None
    decision_status: str | None = None
    decision_comment: str | None = None
    execution_status: str | None = None
    execution_summary: str | None = None
    execution_run_id: str | None = None
    execution_tool_call_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class AgentActionQueueRead(BaseModel):
    pending: list[AgentActionItemRead] = Field(default_factory=list)
    history: list[AgentActionItemRead] = Field(default_factory=list)


class AgentTurnRead(BaseModel):
    session: AgentSessionRead
    user_message: AgentMessageRead
    assistant_message: AgentMessageRead
    run: AgentRunRead
