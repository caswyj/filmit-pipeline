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


class AgentTurnRead(BaseModel):
    session: AgentSessionRead
    user_message: AgentMessageRead
    assistant_message: AgentMessageRead
    run: AgentRunRead
