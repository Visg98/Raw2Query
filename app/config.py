"""Runtime configuration, loaded from environment variables / .env.

Section 7 of the plan: GROQ_API_KEY, DATABASE_URL, embedding model name
etc. are all supplied via .env, loaded through Pydantic settings.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://raw2query:raw2query@localhost:5432/raw2query"
    # Restricted role, SELECT-only on the view_* views (see migrations) - the
    # DB-enforced backstop for NL-to-SQL (plan section 3, call site 11).
    readonly_database_url: str = "postgresql+psycopg://raw2query_query_runner:raw2query_query_runner@localhost:5432/raw2query"
    # Escape hatch for running generated SQL as the read-write role when no
    # restricted role is available. Off by default and never appropriate
    # outside local debugging: with it on, safety rail (b) is gone and only
    # the parser rail stands between a generated statement and the data.
    allow_readwrite_query_fallback: bool = False

    groq_api_key: str = ""
    # Groq serves an OpenAI-compatible surface, which is why the client in
    # app/pipeline/llm.py is still the `openai` SDK pointed at this URL.
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Must be a Groq model that honours `response_format: json_schema` with
    # `strict: true` — every call site depends on it (see llm.py). Verified
    # working: openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b.
    groq_model: str = "openai/gpt-oss-120b"

    embedding_model_name: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    upload_dir: str = "./data/uploads"

    confidence_threshold: float = 0.75

    worker_poll_interval_seconds: float = 1.0
    worker_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
