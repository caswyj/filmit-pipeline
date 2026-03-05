from __future__ import annotations

from pydantic import BaseModel


class StylePresetRead(BaseModel):
    id: str
    label: str
    description: str
    keywords: list[str]
    palette: list[str]
    lighting: str
    rendering: str
    camera_language: str
    motion_feel: str
