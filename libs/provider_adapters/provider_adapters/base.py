from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

StepType = Literal[
    "chunk",
    "script",
    "shot_detail",
    "image",
    "consistency",
    "video",
    "subtitle",
    "tts",
]


@dataclass(slots=True)
class ProviderRequest:
    step: StepType
    model: str
    input: dict[str, Any]
    prompt: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderResponse:
    output: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(ABC):
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def supports(self, step: StepType, model: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def invoke(self, req: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError

    async def get_video_status(self, job_id: str) -> ProviderResponse:
        raise NotImplementedError

    async def download_video(self, job_id: str) -> tuple[bytes, str]:
        raise NotImplementedError

    async def estimate_cost(self, req: ProviderRequest, usage: dict[str, Any] | None = None) -> float:
        if isinstance(usage, dict):
            provider_cost = usage.get("cost")
            if isinstance(provider_cost, (int, float)):
                return round(float(provider_cost), 6)
        token_hint = float(len(str(req.input)) + len(req.prompt or ""))
        return round(token_hint / 2000.0, 4)

    async def health_check(self) -> bool:
        return True
