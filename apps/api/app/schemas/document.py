from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SourceDocumentRead(BaseModel):
    id: str
    project_id: str
    file_name: str
    file_type: str
    storage_key: str | None
    parse_status: str
    page_map: dict
    created_at: datetime

    model_config = {"from_attributes": True}
