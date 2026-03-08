from __future__ import annotations

import asyncio

from provider_adapters.base import ProviderRequest
from provider_adapters.openrouter_adapter import OpenRouterProviderAdapter


def test_openrouter_estimate_cost_prefers_provider_reported_cost() -> None:
    adapter = OpenRouterProviderAdapter({})
    req = ProviderRequest(step="consistency", model="google/gemini-2.5-flash", input={"text": "hello"})

    cost = asyncio.run(
        adapter.estimate_cost(
            req,
            usage={
                "prompt_tokens": 6400,
                "completion_tokens": 200,
                "cost": 0.00251,
                "cost_details": {"upstream_inference_cost": 0.00251},
            },
        )
    )

    assert cost == 0.00251


def test_openrouter_estimate_cost_uses_catalog_pricing_when_provider_cost_missing() -> None:
    adapter = OpenRouterProviderAdapter({})
    adapter.__class__._pricing_map = {
        "google/gemini-2.5-flash": {
            "input": "0.0000003",
            "output": "0.0000025",
            "request": None,
            "image": None,
        }
    }
    req = ProviderRequest(step="consistency", model="google/gemini-2.5-flash", input={"text": "hello"})

    cost = asyncio.run(
        adapter.estimate_cost(
            req,
            usage={
                "prompt_tokens": 6000,
                "completion_tokens": 200,
            },
        )
    )

    assert cost == 0.0023
