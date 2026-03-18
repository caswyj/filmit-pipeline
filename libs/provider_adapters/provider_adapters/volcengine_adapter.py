from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StepType


class VolcengineLASProviderAdapter(ProviderAdapter):
    def __init__(self, supported: dict[StepType, list[str]]) -> None:
        self._supported = supported
        self._api_key = (
            os.getenv("N2V_VOLCENGINE_LAS_API_KEY", "").strip()
            or os.getenv("N2V_VOLCENGINE_API_KEY", "").strip()
            or os.getenv("LAS_API_KEY", "").strip()
        )
        self._base_url = (
            os.getenv("N2V_VOLCENGINE_LAS_BASE_URL", "").strip()
            or os.getenv("N2V_VOLCENGINE_BASE_URL", "").strip()
            or "https://operator.las.cn-shanghai.volces.com/api/v1"
        ).rstrip("/")
        self._timeout = float(os.getenv("N2V_VOLCENGINE_TIMEOUT_SEC", "180"))

    def name(self) -> str:
        return "volcengine"

    def supports(self, step: StepType, model: str) -> bool:
        return model in self._supported.get(step, [])

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def invoke(self, req: ProviderRequest) -> ProviderResponse:
        if not self._api_key:
            raise ValueError("N2V_VOLCENGINE_LAS_API_KEY is not configured")
        if req.step != "video":
            raise ValueError(f"Volcengine LAS adapter does not support step: {req.step}")

        prompt = str(req.input.get("video_prompt") or self._build_prompt(req)).strip()
        if not prompt:
            raise ValueError("volcengine video request requires a prompt")

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        reference_url = self._extract_public_reference_url(req.params.get("input_reference_url"), req.params.get("input_reference_path"))
        if reference_url:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": reference_url},
                    "role": "first_frame",
                }
            )
        # Seedance 1.5 Pro 的 first/last-frame 模式参数约束更严格。
        # 当前先只稳定接入 first_frame reference，避免把上一章节尾帧误当作本镜头目标尾帧，
        # 从而触发 flf2v 模式并导致整个请求参数校验失败。
        last_frame_url = ""

        payload: dict[str, Any] = {
            "model": req.model,
            "content": content,
            "ratio": self._size_to_ratio(str(req.params.get("size") or "1280x720")),
            "duration": self._normalize_duration(req.params.get("seconds", 5), has_reference=bool(reference_url)),
            "watermark": bool(req.params.get("watermark", False)),
        }
        if "generate_audio" in req.params:
            payload["generate_audio"] = bool(req.params.get("generate_audio"))
        else:
            payload["generate_audio"] = False
        if "seed" in req.params:
            payload["seed"] = int(req.params["seed"])
        if "camera_fixed" in req.params:
            payload["camera_fixed"] = bool(req.params["camera_fixed"])
        if "execution_expires_after" in req.params:
            payload["execution_expires_after"] = int(req.params["execution_expires_after"])
        if "return_last_frame" in req.params:
            payload["return_last_frame"] = bool(req.params["return_last_frame"])
        else:
            payload["return_last_frame"] = True

        response = await self._post_json("/contents/generations/tasks", payload)
        task_id = str(response.get("id") or response.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("volcengine video provider did not return a task id")
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": task_id,
            "summary": f"Volcengine video task queued ({task_id})",
            "video_id": task_id,
            "status": "queued",
            "reference_url_used": reference_url,
            "last_frame_url_used": last_frame_url,
            "duration": payload["duration"],
            "ratio": payload["ratio"],
        }
        return ProviderResponse(output=artifact, usage={}, raw=response)

    async def get_video_status(self, job_id: str) -> ProviderResponse:
        response = await self._get_json(f"/contents/generations/tasks/{job_id}")
        status = self._normalize_status(str(response.get("status") or response.get("task_status") or response.get("state") or ""))
        content = self._extract_content(response)
        artifact = {
            "provider": self.name(),
            "step": "video",
            "artifact_id": str(response.get("id") or response.get("task_id") or job_id),
            "video_id": str(response.get("id") or response.get("task_id") or job_id),
            "status": status,
            "progress": response.get("progress") or response.get("percent"),
            "video_url": content.get("video_url"),
            "last_frame_url": content.get("last_frame_url"),
            "cover_url": content.get("cover_url"),
            "duration": content.get("duration") or response.get("duration"),
        }
        usage = self._extract_usage(response)
        return ProviderResponse(output=artifact, usage=usage, raw=response)

    async def download_video(self, job_id: str) -> tuple[bytes, str]:
        status = await self.get_video_status(job_id)
        artifact = status.output or {}
        video_url = str(artifact.get("video_url") or "").strip()
        if not video_url:
            raise ValueError("volcengine task completed but no video_url was returned")
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(video_url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "video/mp4")

    async def estimate_cost(self, req: ProviderRequest, usage: dict[str, Any] | None = None) -> float:
        if isinstance(usage, dict):
            provider_cost = usage.get("cost")
            if isinstance(provider_cost, (int, float)):
                return round(float(provider_cost), 6)
        return 0.0

    def _build_prompt(self, req: ProviderRequest) -> str:
        pieces = []
        if req.prompt:
            pieces.append(req.prompt)
        pieces.append(json.dumps(req.input, ensure_ascii=False, indent=2))
        return "\n\n".join(piece for piece in pieces if piece)

    def _extract_public_reference_url(self, url_value: Any, path_value: Any) -> str:
        for candidate in (url_value, path_value):
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = candidate.strip()
            if not normalized.startswith(("http://", "https://")):
                continue
            parsed = urlparse(normalized)
            hostname = (parsed.hostname or "").lower()
            if hostname in {"127.0.0.1", "0.0.0.0", "localhost"} or hostname.endswith(".local"):
                continue
            return normalized
        return ""

    def _size_to_ratio(self, size: str) -> str:
        try:
            width, height = size.lower().split("x", 1)
            width_value = int(width)
            height_value = int(height)
        except (TypeError, ValueError):
            return "16:9"
        if width_value == height_value:
            return "1:1"
        return "16:9" if width_value >= height_value else "9:16"

    def _normalize_status(self, status: str) -> str:
        normalized = status.strip().lower()
        if normalized in {"succeeded", "success", "completed", "done"}:
            return "completed"
        if normalized in {"failed", "error", "expired", "cancelled", "canceled"}:
            return "failed"
        if normalized in {"running", "processing", "in_progress", "working"}:
            return "running"
        if normalized in {"queued", "pending", "created"}:
            return "queued"
        return normalized or "queued"

    def _normalize_duration(self, raw_value: Any, *, has_reference: bool) -> int:
        try:
            seconds = int(round(float(raw_value)))
        except (TypeError, ValueError):
            seconds = 5
        seconds = max(2, min(seconds, 12))
        if has_reference and seconds < 5:
            return 5
        return seconds

    def _extract_content(self, response: dict[str, Any]) -> dict[str, Any]:
        for key in ("content", "result", "output", "data"):
            value = response.get(key)
            if isinstance(value, dict):
                return value
        return {}

    def _extract_usage(self, response: dict[str, Any]) -> dict[str, Any]:
        usage = response.get("usage")
        if isinstance(usage, dict):
            return usage
        if isinstance(response.get("token_usage"), dict):
            return response["token_usage"]
        return {}

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.post(f"{self._base_url}{path}", headers=headers, json=payload)
        if response.is_error:
            raise ValueError(self._format_error(response))
        return response.json()

    async def _get_json(self, path: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
        if response.is_error:
            raise ValueError(self._format_error(response))
        return response.json()

    def _format_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {}
        code = str(payload.get("code") or payload.get("error_code") or "").strip()
        message = str(payload.get("message") or payload.get("error") or response.text or response.reason_phrase).strip()
        if code and message:
            return f"{response.status_code} from Volcengine LAS: {code} - {message}"
        if code:
            return f"{response.status_code} from Volcengine LAS: {code}"
        if message:
            return f"{response.status_code} from Volcengine LAS: {message}"
        return f"{response.status_code} from Volcengine LAS"
