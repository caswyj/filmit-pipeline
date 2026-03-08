from __future__ import annotations

import base64
from typing import Any

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StepType


class EdgeTTSProviderAdapter(ProviderAdapter):
    def __init__(self, supported: dict[StepType, list[str]]) -> None:
        self._supported = supported

    def name(self) -> str:
        return "edge_tts"

    def is_configured(self) -> bool:
        try:
            import edge_tts  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, step: StepType, model: str) -> bool:
        return step == "tts" and model in self._supported.get(step, [])

    async def invoke(self, req: ProviderRequest) -> ProviderResponse:
        if req.step != "tts":
            raise ValueError(f"edge_tts only supports tts, got {req.step}")
        try:
            import edge_tts
        except Exception as exc:  # noqa: BLE001
            raise ValueError("edge_tts package is not installed") from exc

        direct_text = req.params.get("tts_text") or req.input.get("tts_text")
        text = str(direct_text or "").strip()
        if not text:
            raise ValueError("edge_tts requires non-empty tts_text")

        rate = self._speed_to_rate(req.params.get("speed"))
        volume = str(req.params.get("volume") or "+0%")
        pitch = str(req.params.get("pitch") or "+0Hz")
        communicate = edge_tts.Communicate(text, voice=req.model, rate=rate, volume=volume, pitch=pitch)

        audio_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if isinstance(chunk, dict) and chunk.get("type") == "audio" and isinstance(chunk.get("data"), (bytes, bytearray)):
                audio_chunks.append(bytes(chunk["data"]))

        content = b"".join(audio_chunks)
        if not content:
            raise ValueError("edge_tts did not return audio")

        artifact = {
            "provider": self.name(),
            "step": req.step,
            "model": req.model,
            "artifact_id": f"edge-tts-{req.model}",
            "summary": f"Generated narration with {req.model}",
            "mime_type": "audio/mpeg",
            "audio_base64": base64.b64encode(content).decode("ascii"),
        }
        usage = {
            "inputTokens": max(1, len(text)),
            "outputTokens": 0,
            "characters": len(text),
        }
        return ProviderResponse(output=artifact, usage=usage, raw={"voice": req.model, "rate": rate, "volume": volume, "pitch": pitch})

    def _speed_to_rate(self, speed: Any) -> str:
        if isinstance(speed, str) and speed.endswith("%"):
            return speed
        try:
            numeric = float(speed)
        except (TypeError, ValueError):
            return "+0%"
        percent = round((numeric - 1.0) * 100)
        return f"{percent:+d}%"
