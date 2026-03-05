from __future__ import annotations

import json
import os
import asyncio
from typing import Any

import httpx

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StepType


class OpenRouterProviderAdapter(ProviderAdapter):
    def __init__(self, supported: dict[StepType, list[str]]) -> None:
        self._supported = supported
        self._api_key = os.getenv("N2V_OPENROUTER_API_KEY", "").strip()
        self._api_url = os.getenv(
            "N2V_OPENROUTER_API_URL",
            "https://openrouter.ai/api/v1/chat/completions",
        ).strip()
        self._referer = os.getenv("N2V_OPENROUTER_SITE_URL", "http://localhost:3000").strip()
        self._title = os.getenv("N2V_OPENROUTER_APP_NAME", "Novel-to-Video Pipeline").strip()
        self._timeout = float(os.getenv("N2V_OPENROUTER_TIMEOUT_SEC", "180"))

    def name(self) -> str:
        return "openrouter"

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def supports(self, step: StepType, model: str) -> bool:
        return bool(model.strip())

    async def invoke(self, req: ProviderRequest) -> ProviderResponse:
        if not self._api_key:
            raise ValueError("N2V_OPENROUTER_API_KEY is not configured")

        payload = self._build_payload(req)
        max_tokens = req.params.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_tokens"] = max_tokens

        response = await self._post_json(payload)
        message = self._extract_message(response)
        artifact = self._build_artifact(req, response, message)
        return ProviderResponse(output=artifact, usage=response.get("usage", {}), raw=response)

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        if req.step == "image":
            input_payload = req.input if isinstance(req.input, dict) else {}
            image_prompt = str(
                input_payload.get("prompt")
                or input_payload.get("image_prompt")
                or input_payload.get("text")
                or json.dumps(req.input, ensure_ascii=False, indent=2)
            ).strip()
            content = self._multimodal_content(image_prompt, input_payload.get("reference_images"))
            payload = {
                "model": req.model,
                "messages": [
                    {"role": "system", "content": req.prompt or "Return exactly one image and no explanation."},
                    {"role": "user", "content": content},
                ],
            }
        elif req.step == "consistency" and isinstance(req.input, dict) and req.input.get("visual_inputs"):
            payload = {
                "model": req.model,
                "messages": [
                    {"role": "system", "content": req.prompt or "You are a precise visual consistency reviewer."},
                    {
                        "role": "user",
                        "content": self._multimodal_content(
                            str(req.input.get("text_prompt") or json.dumps(req.input, ensure_ascii=False, indent=2)),
                            req.input.get("visual_inputs"),
                        ),
                    },
                ],
                "temperature": req.params.get("temperature", 0.1),
            }
        else:
            payload = {
                "model": req.model,
                "messages": [
                    {"role": "system", "content": req.prompt or "You are a precise workflow assistant."},
                    {
                        "role": "user",
                        "content": json.dumps(req.input, ensure_ascii=False, indent=2),
                    },
                ],
                "temperature": req.params.get("temperature", 0.3),
            }
        if req.step == "image":
            payload["modalities"] = ["image", "text"]
            image_config: dict[str, Any] = {}
            if req.params.get("aspect_ratio"):
                image_config["aspect_ratio"] = req.params["aspect_ratio"]
            if req.params.get("size"):
                image_config["size"] = req.params["size"]
            if image_config:
                payload["image_config"] = image_config
        return payload

    def _multimodal_content(self, text_prompt: str, images: Any) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text_prompt}]
        for image in images or []:
            if not isinstance(image, dict):
                continue
            url = image.get("url") or image.get("image_url") or image.get("image_data_url")
            if not isinstance(url, str) or not url:
                continue
            content.append({"type": "image_url", "image_url": {"url": url}})
        return content

    def _build_artifact(self, req: ProviderRequest, response: dict[str, Any], message: str) -> dict[str, Any]:
        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": response.get("id"),
            "summary": clip_text(message, 300) or f"{req.step} generated by OpenRouter",
            "text": message,
        }
        if req.step == "image":
            image_url = self._extract_image_data_url(response)
            if image_url and image_url.startswith("data:"):
                artifact["image_data_url"] = image_url
            else:
                artifact["image_url"] = image_url
            artifact["artifact_mode"] = "image"
        else:
            artifact["artifact_mode"] = "prompt_only"
        return artifact

    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._referer,
            "X-Title": self._title,
        }
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._api_url, headers=headers, json=payload)
                break
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= 3:
                    raise
                await asyncio.sleep(1.2 * attempt)
        else:
            raise last_exc or RuntimeError("OpenRouter request failed without response")
        if response.is_error:
            detail = response.text.strip()
            if len(detail) > 800:
                detail = f"{detail[:800]}..."
            raise httpx.HTTPStatusError(
                f"{response.status_code} from OpenRouter: {detail or 'empty error body'}",
                request=response.request,
                response=response,
            )
        return response.json()

    def _extract_message(self, response: dict[str, Any]) -> str:
        choices = response.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts).strip()
        return str(content or "").strip()

    def _extract_image_data_url(self, response: dict[str, Any]) -> str | None:
        choices = response.get("choices") or []
        if not choices:
            return None
        message = (choices[0] or {}).get("message") or {}
        images = message.get("images") or []
        if not isinstance(images, list) or not images:
            return None
        first = images[0] or {}
        if not isinstance(first, dict):
            return None
        image_url = first.get("image_url") or {}
        if isinstance(image_url, dict):
            url = image_url.get("url")
            return str(url) if isinstance(url, str) and url else None
        return None


def clip_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."
