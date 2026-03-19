from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="N2V_", extra="ignore")

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "sqlite:///./n2v.db"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "n2v-assets"
    tos_access_key_id: str | None = None
    tos_secret_access_key: str | None = None
    tos_session_token: str | None = None
    tos_region: str | None = None
    tos_endpoint: str | None = None
    tos_bucket: str | None = None
    tos_presign_expire_sec: int = 3600
    secret_key: str = "change-me"
    consistency_threshold: int = 75
    demo_1408_path: str | None = None
    generated_dir: str = "/Users/wyj/proj/novel-to-video-demo-cases"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_sec: int = 180
    openrouter_api_key: str | None = None
    openrouter_api_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_site_url: str = "http://localhost:3000"
    openrouter_app_name: str = "FilmIt Pipeline"
    openrouter_timeout_sec: int = 180
    volcengine_las_api_key: str | None = None
    volcengine_las_base_url: str = "https://operator.las.cn-shanghai.volces.com/api/v1"
    volcengine_timeout_sec: int = 180
    agent_provider: str = "openrouter"
    agent_model_name: str = "openai/gpt-5-mini"
    agent_live_model_enabled: bool = True
    agent_max_output_tokens: int = 900
    agent_openrouter_api_key: str | None = None
    agent_openrouter_api_url: str | None = None
    agent_openrouter_site_url: str | None = None
    agent_openrouter_app_name: str | None = None
    agent_openrouter_timeout_sec: int | None = None
    video_poll_interval_sec: int = 8
    video_poll_max_attempts: int = 15


settings = Settings()
