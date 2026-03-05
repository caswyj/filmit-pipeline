from __future__ import annotations

import os

from celery import Celery

redis_url = os.getenv("N2V_REDIS_URL", "redis://localhost:6379/0")
app = Celery("n2v-media-worker", broker=redis_url, backend=redis_url)


@app.task(name="n2v.media.ping")
def ping() -> str:
    return "media-worker-ok"


if __name__ == "__main__":
    app.worker_main(["worker", "--loglevel=info", "-Q", "media"])
