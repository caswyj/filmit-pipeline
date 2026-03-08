from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.request import Request, urlopen

_OPENROUTER_MODELS_CACHE: list[dict[str, Any]] | None = None
_OPENROUTER_MODELS_CACHE_TS: float = 0.0
_OPENROUTER_MODELS_CACHE_TTL_SEC: int = 600


def fetch_openrouter_models() -> list[dict[str, Any]]:
    global _OPENROUTER_MODELS_CACHE, _OPENROUTER_MODELS_CACHE_TS

    now = time.time()
    if _OPENROUTER_MODELS_CACHE is not None and now - _OPENROUTER_MODELS_CACHE_TS < _OPENROUTER_MODELS_CACHE_TTL_SEC:
        return _OPENROUTER_MODELS_CACHE

    try:
        api_key = os.getenv("N2V_OPENROUTER_API_KEY", "").strip()
        payload = None
        if api_key:
            req = Request(
                "https://openrouter.ai/api/v1/models/user",
                headers={
                    "User-Agent": "n2v-provider-registry/1.0",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        if payload is None:
            req = Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "n2v-provider-registry/1.0"})
            with urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data") or []
        if isinstance(models, list):
            _OPENROUTER_MODELS_CACHE = models
            _OPENROUTER_MODELS_CACHE_TS = now
            return models
    except Exception:  # noqa: BLE001
        return []
    return []


def build_openrouter_pricing_map(models: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    pricing_map: dict[str, dict[str, Any]] = {}
    for model in models or fetch_openrouter_models():
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            continue
        pricing = model.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        pricing_map[model_id] = {
            "input": pricing.get("prompt"),
            "output": pricing.get("completion"),
            "request": pricing.get("request"),
            "image": pricing.get("image"),
        }
    return pricing_map


def parse_openrouter_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None
