from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProviderModelRead(BaseModel):
    provider: str
    step: str
    models: list[str]
    model_pricing: dict[str, dict[str, Any]] | None = None
