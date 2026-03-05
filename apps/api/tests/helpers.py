from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


def fresh_app(
    *,
    database_url: str,
    demo_1408_path: str | None = None,
    consistency_threshold: int | None = None,
):
    os.environ["N2V_DATABASE_URL"] = database_url
    generated_root = str((Path(database_url.replace("sqlite:///", "")).parent / "generated").resolve())
    os.environ["N2V_GENERATED_DIR"] = generated_root
    if demo_1408_path is not None:
        os.environ["N2V_DEMO_1408_PATH"] = demo_1408_path
    elif "N2V_DEMO_1408_PATH" in os.environ:
        del os.environ["N2V_DEMO_1408_PATH"]
    if consistency_threshold is not None:
        os.environ["N2V_CONSISTENCY_THRESHOLD"] = str(consistency_threshold)
    elif "N2V_CONSISTENCY_THRESHOLD" in os.environ:
        del os.environ["N2V_CONSISTENCY_THRESHOLD"]

    for provider_key in ("N2V_OPENAI_API_KEY", "N2V_OPENROUTER_API_KEY"):
        if provider_key in os.environ:
            del os.environ[provider_key]

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]

    importlib.invalidate_caches()

    from app.main import app

    return app
