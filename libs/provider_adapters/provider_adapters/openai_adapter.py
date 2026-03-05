from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StepType


class OpenAIProviderAdapter(ProviderAdapter):
    def __init__(self, supported: dict[StepType, list[str]]) -> None:
        self._supported = supported
        self._api_key = os.getenv("N2V_OPENAI_API_KEY", "").strip()
        self._base_url = os.getenv("N2V_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self._timeout = float(os.getenv("N2V_OPENAI_TIMEOUT_SEC", "180"))

    def name(self) -> str:
        return "openai"

    def supports(self, step: StepType, model: str) -> bool:
        return model in self._supported.get(step, [])

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def invoke(self, req: ProviderRequest) -> ProviderResponse:
        if not self._api_key:
            raise ValueError("N2V_OPENAI_API_KEY is not configured")

        if req.step in {"chunk", "script", "shot_detail", "consistency"}:
            return await self._invoke_text(req)
        if req.step == "image":
            return await self._invoke_image(req)
        if req.step == "video":
            return await self._invoke_video(req)
        if req.step == "tts":
            return await self._invoke_tts(req)
        raise ValueError(f"OpenAI adapter does not support step: {req.step}")

    async def _invoke_text(self, req: ProviderRequest) -> ProviderResponse:
        payload = {
            "model": req.model,
            "instructions": req.prompt or "",
            "input": json.dumps(req.input, ensure_ascii=False, indent=2),
            "max_output_tokens": int(req.params.get("max_output_tokens", 1200)),
        }
        response = await self._post_json("/responses", payload)
        output_text = response.get("output_text") or self._collect_response_text(response)
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": response.get("id"),
            "summary": output_text[:300] if output_text else f"{req.step} response created",
            "text": output_text,
        }
        usage = response.get("usage", {})
        return ProviderResponse(output=artifact, usage=usage, raw={"response_id": response.get("id")})

    async def _invoke_image(self, req: ProviderRequest) -> ProviderResponse:
        prompt = self._build_prompt(req)
        payload = {
            "model": req.model,
            "prompt": prompt,
            "size": req.params.get("size", "1024x1024"),
            "quality": req.params.get("quality", "medium"),
            "background": req.params.get("background", "auto"),
            "output_format": req.params.get("output_format", "png"),
        }
        response = await self._post_json("/images/generations", payload)
        image_item = (response.get("data") or [{}])[0]
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": response.get("created") or image_item.get("revised_prompt", "")[:24],
            "summary": clip_prompt(prompt),
            "revised_prompt": image_item.get("revised_prompt"),
            "mime_type": "image/png" if payload["output_format"] == "png" else f"image/{payload['output_format']}",
            "image_base64": image_item.get("b64_json"),
        }
        return ProviderResponse(output=artifact, usage={}, raw={"created": response.get("created")})

    async def _invoke_video(self, req: ProviderRequest) -> ProviderResponse:
        data = {
            "model": req.model,
            "prompt": str(req.input.get("video_prompt") or self._build_prompt(req)),
            "seconds": str(req.params.get("seconds", 4)),
            "size": req.params.get("size", "1280x720"),
        }
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        reference_path = req.params.get("input_reference_path")
        if isinstance(reference_path, str) and reference_path:
            path = os.path.expanduser(reference_path)
            if os.path.exists(path):
                mime_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
                files.append(("input_reference", (os.path.basename(path), Path(path).read_bytes(), mime_type)))
        response = await self._post_form("/videos", data, files=files or None)
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": response.get("id"),
            "summary": f"Video job {response.get('status', 'queued')}",
            "video_id": response.get("id"),
            "status": response.get("status"),
            "seconds": response.get("seconds"),
            "size": response.get("size"),
            "progress": response.get("progress"),
        }
        return ProviderResponse(output=artifact, usage={}, raw={"video_id": response.get("id")})

    async def get_video_status(self, job_id: str) -> ProviderResponse:
        response = await self._get_json(f"/videos/{job_id}")
        artifact = {
            "provider": self.name(),
            "step": "video",
            "model": response.get("model"),
            "artifact_id": response.get("id"),
            "video_id": response.get("id"),
            "status": response.get("status"),
            "seconds": response.get("seconds"),
            "size": response.get("size"),
            "progress": response.get("progress"),
        }
        return ProviderResponse(output=artifact, usage={}, raw=response)

    async def download_video(self, job_id: str) -> tuple[bytes, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}/videos/{job_id}/content", headers=headers)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "video/mp4")

    async def _invoke_tts(self, req: ProviderRequest) -> ProviderResponse:
        text = self._build_prompt(req)
        payload = {
            "model": req.model,
            "voice": req.params.get("voice", "alloy"),
            "input": text[:4096],
            "response_format": req.params.get("format", "mp3"),
            "instructions": req.params.get("instructions"),
            "speed": req.params.get("speed", 1.0),
        }
        content, headers = await self._post_binary("/audio/speech", payload)
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": headers.get("x-request-id", "tts-response"),
            "summary": clip_prompt(text),
            "mime_type": headers.get("content-type", "audio/mpeg"),
            "audio_base64": base64.b64encode(content).decode("ascii"),
        }
        return ProviderResponse(output=artifact, usage={}, raw={"content_type": headers.get("content-type")})

    def _build_prompt(self, req: ProviderRequest) -> str:
        pieces = []
        if req.prompt:
            pieces.append(req.prompt)
        pieces.append(json.dumps(req.input, ensure_ascii=False, indent=2))
        return "\n\n".join(piece for piece in pieces if piece)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}{path}", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    async def _post_binary(self, path: str, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}{path}", headers=headers, json=payload)
        response.raise_for_status()
        return response.content, dict(response.headers)

    async def _get_json(self, path: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
        response.raise_for_status()
        return response.json()

    async def _post_form(self, path: str, data: dict[str, Any], files: list[tuple[str, tuple[str, bytes, str]]] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}{path}", headers=headers, data=data, files=files)
        response.raise_for_status()
        raw = response.json()
        if isinstance(raw, dict) and isinstance(raw.get("data"), list) and raw["data"]:
            first = raw["data"][0]
            if isinstance(first, dict):
                return first
        return raw

    def _collect_response_text(self, response: dict[str, Any]) -> str:
        lines: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    lines.append(content["text"])
        return "\n".join(lines).strip()


def clip_prompt(value: str, limit: int = 180) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."
